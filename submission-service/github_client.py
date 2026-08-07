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
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import socket
from pathlib import Path

MAX_LIST_PAGES = 20
MAX_LIST_ITEMS = 5000
MAX_DOWNLOAD_BYTES = 110 * 1024 * 1024
MAX_REDIRECTS = 5
# Test-only override; production requires https Link URLs on the same host.
_ALLOW_INSECURE_LINKS = False
# Test-only override; production rejects non-HTTPS asset redirects.
_ALLOW_INSECURE_DOWNLOADS = False


class _SensitiveStripRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow asset redirects but never forward sensitive headers cross-host."""

    SENSITIVE_HEADERS = ("Authorization", "Cookie", "Proxy-Authorization")
    max_redirects = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_request is None:
            return None
        old_host = urllib.parse.urlsplit(req.full_url).netloc
        new_host = urllib.parse.urlsplit(newurl).netloc
        if (
            urllib.parse.urlsplit(newurl).scheme != "https"
            and not _ALLOW_INSECURE_DOWNLOADS
        ):
            return None
        if new_host != old_host:
            for name in self.SENSITIVE_HEADERS:
                new_request.remove_header(name)
        return new_request

class GitHubApiError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 0,
                 retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.status in (429, 500, 502, 503, 504) or self.status == 0


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
                 raw: bytes | None = None, content_type: str | None = None,
                 retries: int = 3) -> dict:
        data, _headers = self._request_full(
            method, url, token, payload, raw, content_type, retries
        )
        return data

    def _request_full(self, method: str, url: str, token: str, payload=None,
                      raw: bytes | None = None, content_type: str | None = None,
                      retries: int = 3):
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
        attempt = 0
        while True:
            request = urllib.request.Request(
                url, data=data, headers=headers, method=method
            )
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    body = response.read()
                    if not body:
                        return {}, dict(response.headers)
                    return json.loads(body.decode("utf-8")), dict(response.headers)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:500]
                exc.close()
                retry_after = None
                header_value = exc.headers.get("Retry-After")
                if header_value:
                    try:
                        retry_after = float(header_value)
                    except ValueError:
                        retry_after = None
                try:
                    parsed = json.loads(detail)
                    message = parsed.get("message", detail)
                except json.JSONDecodeError:
                    message = detail
                api_error = GitHubApiError(
                    "github_api_error", message, exc.code, retry_after
                )
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                api_error = GitHubApiError(
                    "github_timeout",
                    "GitHub request timed out or failed to connect",
                )
            if (
                method == "GET"
                and api_error.retryable
                and attempt + 1 < retries
            ):
                delay = api_error.retry_after
                if delay is None:
                    delay = min(1.5 * (2 ** attempt), 30)
                time.sleep(min(delay, 30))
                attempt += 1
                continue
            raise api_error

    def _parse_link_next(self, link_header: str, current_url: str) -> str | None:
        if not link_header:
            return None
        expected_host = urllib.parse.urlsplit(current_url).netloc
        for part in link_header.split(","):
            match = re.match(r"\s*<([^>]+)>\s*;\s*rel=\"next\"", part)
            if not match:
                continue
            next_url = match.group(1)
            parsed = urllib.parse.urlsplit(next_url)
            if (parsed.scheme != "https" or parsed.netloc != expected_host) and not _ALLOW_INSECURE_LINKS:
                raise GitHubApiError(
                    "github_pagination_invalid",
                    "pagination Link next URL is not https on the same host",
                )
            return next_url
        return None

    def _request_paged(self, first_url: str, token: str) -> list[list[dict]]:
        """Follow Link headers with hard limits; fails closed on anomalies."""
        pages: list[list[dict]] = []
        visited: set[str] = set()
        url: str | None = first_url
        page_number = 0
        while url is not None:
            if url in visited:
                raise GitHubApiError(
                    "github_pagination_cycle", "pagination Link cycle detected"
                )
            visited.add(url)
            page_number += 1
            if page_number > MAX_LIST_PAGES:
                raise GitHubApiError(
                    "github_pagination_overflow",
                    f"pagination exceeded {MAX_LIST_PAGES} pages",
                )
            data, response_headers = self._request_full("GET", url, token)
            if not isinstance(data, list):
                raise GitHubApiError(
                    "github_pagination_invalid",
                    f"expected a list from {url}",
                )
            pages.append(data)
            url = self._parse_link_next(response_headers.get("Link", ""), url)
        total = sum(len(page) for page in pages)
        if total > MAX_LIST_ITEMS:
            raise GitHubApiError(
                "github_pagination_overflow",
                f"list exceeded {MAX_LIST_ITEMS} items",
            )
        return pages

    def get_user(self, access_token: str) -> dict:
        return self._request("GET", f"{self.api_base}/user", access_token)

    def get_release(self, token: str, release_id: int) -> dict | None:
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}/releases/{release_id}"
        try:
            return self._request("GET", url, token)
        except GitHubApiError as exc:
            if exc.status == 404:
                return None
            raise

    def get_release_by_tag(self, token: str, tag: str) -> dict | None:
        quoted = urllib.parse.quote(tag, safe="")
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}/releases/tags/{quoted}"
        try:
            return self._request("GET", url, token)
        except GitHubApiError as exc:
            if exc.status == 404:
                return None
            raise

    def get_release_assets(self, token: str, release_id: int) -> list[dict]:
        url = (
            f"{self.api_base}/repos/{self.owner}/{self.repo}/releases/"
            f"{release_id}/assets?per_page=100"
        )
        return [
            item
            for page in self._request_paged(url, token)
            for item in page
        ]

    def download_release_asset(self, token: str, release_id: int, asset_id: int,
                               dest: Path, expected_sha256: str,
                               expected_size: int | None = None) -> None:
        """Download a (possibly draft) asset through the authenticated API.

        Streams with a size cap, verifies SHA-256 (and size when given) and
        deletes the destination on any failure. Sensitive headers are never
        forwarded to a different host.
        """
        if not re.match(r"^[0-9a-f]{64}$", expected_sha256):
            raise GitHubApiError(
                "asset_sha256_invalid",
                "expected SHA-256 must be 64 lowercase hex chars",
            )
        url = (
            f"{self.api_base}/repos/{self.owner}/{self.repo}/releases/assets/"
            f"{asset_id}"
        )
        request = urllib.request.Request(url, method="GET")
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("Accept", "application/octet-stream")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        opener = urllib.request.build_opener(_SensitiveStripRedirectHandler())
        hasher = hashlib.sha256()
        written = 0
        try:
            with opener.open(request, timeout=120) as response, dest.open("wb") as out:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_DOWNLOAD_BYTES:
                        raise GitHubApiError(
                            "asset_too_large",
                            "downloaded asset exceeds the size limit",
                        )
                    hasher.update(chunk)
                    out.write(chunk)
        except Exception:
            dest.unlink(missing_ok=True)
            raise
        actual = hasher.hexdigest()
        if actual.lower() != expected_sha256.lower():
            dest.unlink(missing_ok=True)
            raise GitHubApiError(
                "asset_sha256_mismatch",
                "downloaded asset SHA-256 does not match the manifest",
            )
        if expected_size is not None and written != expected_size:
            dest.unlink(missing_ok=True)
            raise GitHubApiError(
                "asset_size_mismatch",
                f"downloaded asset size {written} does not match {expected_size}",
            )

    def get_pull_request(self, token: str, pr_number: int) -> dict:
        url = (
            f"{self.api_base}/repos/{self.owner}/{self.repo}/pulls/"
            f"{pr_number}"
        )
        return self._request("GET", url, token)

    def get_check_runs_for_head(self, token: str, head_sha: str) -> list[dict]:
        url = (
            f"{self.api_base}/repos/{self.owner}/{self.repo}/commits/"
            f"{head_sha}/check-runs?per_page=100"
        )
        runs: list[dict] = []
        visited: set[str] = set()
        page_number = 0
        while url is not None:
            if url in visited:
                raise GitHubApiError(
                    "github_pagination_cycle",
                    "pagination Link cycle detected",
                )
            visited.add(url)
            page_number += 1
            if page_number > MAX_LIST_PAGES:
                raise GitHubApiError(
                    "github_pagination_overflow",
                    f"pagination exceeded {MAX_LIST_PAGES} pages",
                )
            data, response_headers = self._request_full("GET", url, token)
            if (not isinstance(data, dict)
                    or not isinstance(data.get("check_runs"), list)):
                raise GitHubApiError(
                    "github_api_invalid",
                    "check-runs response is malformed",
                )
            runs.extend(data["check_runs"])
            url = self._parse_link_next(
                response_headers.get("Link", ""), url
            )
        return runs

    def create_check_run(self, token: str, head_sha: str, name: str,
                         external_id: str, output: dict | None = None) -> dict:
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}/check-runs"
        payload: dict = {
            "name": name,
            "head_sha": head_sha,
            "status": "in_progress",
            "external_id": external_id,
        }
        if output:
            payload["output"] = output
        return self._request("POST", url, token, payload)

    def update_check_run(self, token: str, check_run_id: int,
                         status: str | None = None,
                         conclusion: str | None = None,
                         output: dict | None = None) -> dict:
        url = (
            f"{self.api_base}/repos/{self.owner}/{self.repo}/check-runs/"
            f"{check_run_id}"
        )
        payload: dict = {}
        if status:
            payload["status"] = status
        if conclusion:
            payload["conclusion"] = conclusion
        if output:
            payload["output"] = output
        return self._request("PATCH", url, token, payload)

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

    def get_branch_ref(self, token: str, branch: str) -> dict | None:
        quoted = urllib.parse.quote(f"heads/{branch}", safe="")
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}/git/ref/{quoted}"
        try:
            return self._request("GET", url, token)
        except GitHubApiError as exc:
            if exc.status == 404:
                return None
            raise

    def delete_branch_ref(self, token: str, branch: str) -> None:
        quoted = urllib.parse.quote(f"heads/{branch}", safe="/")
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}/git/refs/{quoted}"
        try:
            self._request("DELETE", url, token)
        except GitHubApiError as exc:
            if exc.status != 404:
                raise

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

    def get_pull_request_by_head(self, token: str, head: str) -> dict | None:
        query = urllib.parse.urlencode(
            {"state": "open", "head": f"{self.owner}:{head}", "per_page": "100"}
        )
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}/pulls?{query}"
        for page in self._request_paged(url, token):
            if page:
                return page[0]
        return None

    def get_pull_request_files(self, token: str, pr_number: int) -> list[str]:
        url = (
            f"{self.api_base}/repos/{self.owner}/{self.repo}/pulls/"
            f"{pr_number}/files?per_page=100"
        )
        return [
            entry.get("filename", "")
            for page in self._request_paged(url, token)
            for entry in page
        ]
