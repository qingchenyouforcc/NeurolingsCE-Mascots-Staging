"""Workflow helpers used by GitHub Actions (stdlib-only)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path

SAFE_DOWNLOAD_HOSTS = ("github.com", "objects.githubusercontent.com")
MAX_DOWNLOAD_BYTES = 110 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_PAGES = 20
MAX_LIST_ITEMS = 5000
# Test-only override; production always rejects non-HTTPS redirects.
_ALLOW_INSECURE_REDIRECTS = False
# Populated only while an asset download is in flight, so the redirect
# handler can record sanitized hops (hosts/status, never headers).
_ACTIVE_DOWNLOAD_TRACE: list | None = None
FILE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.mascot$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SUBMISSION_ID_RE = re.compile(r"^[0-9a-f]{24}$")

SUBMISSION_BRANCH_RE = re.compile(
    r"^submission/([a-z0-9]+(?:-[a-z0-9]+)*)-"
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$"
)
MANIFEST_PATH_RE = re.compile(
    r"^mascots/([a-z0-9]+(?:-[a-z0-9]+)*)/manifest\.json$"
)


class WorkflowApiError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class _SensitiveStrippingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects but never forward sensitive headers cross-host."""

    SENSITIVE_HEADERS = ("Authorization", "Cookie", "Proxy-Authorization")
    max_redirects = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_request is None:
            return None
        old_host = urllib.parse.urlsplit(req.full_url).netloc
        new_host = urllib.parse.urlsplit(newurl).netloc
        if urllib.parse.urlsplit(newurl).scheme != "https" and not _ALLOW_INSECURE_REDIRECTS:
            return None
        if _ACTIVE_DOWNLOAD_TRACE is not None:
            _ACTIVE_DOWNLOAD_TRACE.append({
                "status": code,
                "fromHost": old_host,
                "toHost": new_host,
                "authorizationForwarded": old_host == new_host,
            })
        if new_host != old_host:
            for name in self.SENSITIVE_HEADERS:
                new_request.remove_header(name)
        return new_request


def _build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_SensitiveStrippingRedirectHandler())


def _github_open_with_headers(method: str, url: str, token: str,
                              payload: dict | None = None):
    """Open a GitHub API URL and return (parsed_json, response_headers)."""
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _build_opener().open(request, timeout=120) as response:
            body = response.read()
            if not body:
                return {}, dict(response.headers)
            return json.loads(body.decode("utf-8")), dict(response.headers)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise WorkflowApiError(
            f"GitHub API {exc.code} for {url}: {detail}", exc.code
        ) from exc
    except urllib.error.URLError as exc:
        raise WorkflowApiError(f"GitHub request failed for {url}: {exc}") from exc


def github_request_with_headers(method: str, url: str, token: str,
                                payload: dict | None = None):
    return _github_open_with_headers(method, url, token, payload)


def changed_manifest_paths(repo_root: Path, base: str = "origin/main") -> list[Path]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AM", base, "--", "mascots/"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # Fall back to listing all manifests when the base ref is unavailable.
        return list((repo_root / "mascots").glob("*/manifest.json"))
    paths = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or not line.endswith("manifest.json"):
            continue
        path = repo_root / line
        if path.is_file():
            paths.append(path)
    return paths


def load_manifest(manifest_path: Path) -> dict:
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def package_url_from_manifest(manifest: dict) -> str:
    package = manifest.get("package", {})
    url = package.get("url", "")
    if not url:
        raise ValueError("manifest package.url is missing")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise ValueError("manifest package.url must use https")
    return parsed.geturl()


def safe_download(url: str, token: str, dest: Path, expected_sha256: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("https",):
        raise ValueError("download URL must use https")
    if parsed.hostname not in SAFE_DOWNLOAD_HOSTS:
        raise ValueError(f"download URL host is not allowlisted: {parsed.hostname}")
    request = urllib.request.Request(url, method="GET")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
    try:
        _stream_request(request, dest, expected_sha256)
    except Exception:
        dest.unlink(missing_ok=True)
        raise


def _stream_request(request, dest: Path, expected_sha256: str) -> None:
    """Stream a request body to disk with size cap and SHA-256 verification."""
    hasher = hashlib.sha256()
    written = 0
    try:
        with _build_opener().open(request, timeout=120) as response, dest.open("wb") as out:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_DOWNLOAD_BYTES:
                    raise ValueError("downloaded package exceeds size limit")
                hasher.update(chunk)
                out.write(chunk)
        actual = hasher.hexdigest()
        if actual.lower() != expected_sha256.lower():
            raise ValueError(
                f"SHA-256 mismatch: expected {expected_sha256}, got {actual}"
            )
    except Exception:
        dest.unlink(missing_ok=True)
        raise


def github_request(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    data, _headers = _github_open_with_headers(method, url, token, payload)
    return data


def get_pull_request(token: str, owner: str, repo: str, pr_number: int) -> dict:
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    return github_request("GET", url, token)


def _parse_link_next(link_header: str, current_url: str) -> str | None:
    """Extract the 'next' URL from a Link header (safely)."""
    if not link_header:
        return None
    expected_host = urllib.parse.urlsplit(current_url).netloc
    for part in link_header.split(","):
        match = re.match(r"\s*<([^>]+)>\s*;\s*rel=\"next\"", part)
        if not match:
            continue
        next_url = match.group(1)
        parsed = urllib.parse.urlsplit(next_url)
        if parsed.scheme != "https" or parsed.netloc != expected_host:
            raise WorkflowApiError(
                f"pagination Link next URL is not https/{expected_host}: {next_url!r}"
            )
        return next_url
    return None


def _iter_pages(first_url: str, token: str) -> list[list[dict]]:
    """Follow Link headers; returns pages. Fails closed on anomalies."""
    pages: list[list[dict]] = []
    visited: set[str] = set()
    url: str | None = first_url
    page_number = 0
    while url is not None:
        if url in visited:
            raise WorkflowApiError("pagination Link cycle detected")
        visited.add(url)
        page_number += 1
        if page_number > MAX_PAGES:
            raise WorkflowApiError(f"pagination exceeded {MAX_PAGES} pages")
        data, headers = github_request_with_headers("GET", url, token)
        if not isinstance(data, list):
            raise WorkflowApiError(f"expected a list from {url}, got {type(data).__name__}")
        pages.append(data)
        url = _parse_link_next(headers.get("Link", ""), url)
    total = sum(len(page) for page in pages)
    if total > MAX_LIST_ITEMS:
        raise WorkflowApiError(f"list exceeded {MAX_LIST_ITEMS} items")
    return pages


def get_pr_files(token: str, owner: str, repo: str, pr_number: int,
                 expected_count: int | None = None) -> list[dict]:
    first_url = (
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
        f"?per_page=100"
    )
    files = [
        item
        for page in _iter_pages(first_url, token)
        for item in page
    ]
    if expected_count is not None and len(files) != expected_count:
        raise WorkflowApiError(
            f"PR {pr_number} changed_files count {expected_count} does not "
            f"match the {len(files)} files returned by the API"
        )
    return files


def get_file_at_ref(token: str, owner: str, repo: str, path: str,
                    ref: str) -> dict | None:
    quoted_ref = urllib.parse.quote(ref, safe="")
    quoted_path = urllib.parse.quote(path, safe="/")
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{quoted_path}?ref={quoted_ref}"
    try:
        return github_request("GET", url, token)
    except WorkflowApiError as exc:
        if exc.status == 404:
            return None
        raise


def decode_contents(entry: dict) -> str:
    import base64
    content = entry.get("content", "")
    if entry.get("encoding") == "base64":
        return base64.b64decode(content).decode("utf-8")
    return content


def get_release_by_id(token: str, owner: str, repo: str, release_id: int) -> dict | None:
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/{release_id}"
    try:
        return github_request("GET", url, token)
    except WorkflowApiError as exc:
        if exc.status == 404:
            return None
        raise


def get_release_by_id_traced(token: str, owner: str, repo: str,
                             release_id: int) -> tuple[int, dict | None]:
    """Return (http_status, release) without hiding 4xx/5xx statuses."""
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/{release_id}"
    try:
        data, _headers = github_request_with_headers("GET", url, token)
        return 200, data
    except WorkflowApiError as exc:
        return exc.status, None


def get_release_assets(token: str, owner: str, repo: str,
                       release_id: int) -> list[dict]:
    first_url = (
        f"https://api.github.com/repos/{owner}/{repo}/releases/"
        f"{release_id}/assets?per_page=100"
    )
    return [
        item
        for page in _iter_pages(first_url, token)
        for item in page
    ]


def get_release_assets_traced(token: str, owner: str, repo: str,
                              release_id: int) -> tuple[int, list[dict]]:
    """Return (http_status, assets); pagination anomalies fail closed."""
    first_url = (
        f"https://api.github.com/repos/{owner}/{repo}/releases/"
        f"{release_id}/assets?per_page=100"
    )
    try:
        pages = _iter_pages(first_url, token)
    except WorkflowApiError as exc:
        return exc.status, []
    return 200, [item for page in pages for item in page]


def list_published_release_tags(token: str, owner: str, repo: str) -> list[str]:
    """Return tag names of every non-draft release (paginated)."""
    tags: list[str] = []
    first_url = (
        f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=100"
    )
    for page in _iter_pages(first_url, token):
        for release in page:
            if release.get("draft") is False:
                tag = release.get("tag_name")
                if isinstance(tag, str) and tag:
                    tags.append(tag)
    return sorted(set(tags))


def download_release_asset(token: str, owner: str, repo: str, asset_id: int,
                           dest: Path, expected_sha256: str) -> dict:
    """Download a (possibly draft) release asset through the authenticated API.

    Returns a sanitized trace: {apiStatus, finalStatus, finalHost,
    authorizationForwardedToFinalHost, hops}. No header values are recorded.
    """
    global _ACTIVE_DOWNLOAD_TRACE
    if not SHA256_RE.match(expected_sha256):
        raise ValueError("expected_sha256 must be 64 lowercase hex chars")
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{asset_id}"
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/octet-stream")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    trace: list[dict] = []
    _ACTIVE_DOWNLOAD_TRACE = trace
    try:
        _stream_request(request, dest, expected_sha256)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    finally:
        _ACTIVE_DOWNLOAD_TRACE = None
    api_host = urllib.parse.urlsplit(url).netloc
    if trace:
        first_status = trace[0]["status"]
        final_host = trace[-1]["toHost"]
    else:
        first_status = 200
        final_host = api_host
    return {
        "apiStatus": first_status,
        "finalStatus": 200,
        "finalHost": final_host,
        "authorizationForwardedToFinalHost": any(
            hop["authorizationForwarded"] is True
            and hop["fromHost"] != hop["toHost"]
            for hop in trace
        ),
        "hops": trace,
    }


def publish_release(token: str, owner: str, repo: str, release_id: int) -> dict:
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/{release_id}"
    return github_request("PATCH", url, token, {"draft": False})


def verify_release_before_publish(token: str, owner: str, repo: str,
                                  release_id: int, manifest: dict) -> str:
    """Verify a release is safe to publish; returns 'draft' or 'already_published'.

    Raises WorkflowApiError/ValueError on any mismatch so the workflow fails
    closed instead of touching an unrelated or already-final release.
    """
    mid = manifest.get("id", "")
    version = manifest.get("version", "")
    meta = manifest.get("release") or {}
    expected_tag = f"draft/{mid}-{version}"
    if meta.get("tag") != expected_tag:
        raise ValueError(
            f"manifest release.tag {meta.get('tag')!r} does not match "
            f"{expected_tag!r}"
        )
    release = get_release_by_id(token, owner, repo, release_id)
    if release is None:
        raise WorkflowApiError(
            f"release {release_id} referenced by {mid} {version} does not exist",
            404,
        )
    if release.get("tag_name") != expected_tag:
        raise WorkflowApiError(
            f"release {release_id} tag {release.get('tag_name')!r} does not "
            f"match manifest tag {expected_tag!r}"
        )
    if release.get("draft") is True:
        return "draft"
    if release.get("draft") is False:
        return "already_published"
    raise WorkflowApiError(
        f"release {release_id} has unknown draft state {release.get('draft')!r}"
    )


def delete_release(token: str, owner: str, repo: str, release_id: int) -> None:
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/{release_id}"
    try:
        github_request("DELETE", url, token)
    except WorkflowApiError as exc:
        if exc.status != 404:
            raise


def verify_and_cleanup_submission_pr(token: str, owner: str, repo: str,
                                     pr_number: int) -> dict:
    """Verify a closed PR is a server-shaped submission and delete its draft release.

    Nothing is deleted unless every check passes: PR state, head repository,
    server-generated branch name, allowlisted changed files, manifest identity,
    release metadata and the release's live draft/tag state.
    """
    result: dict = {
        "verified": False,
        "reason": "",
        "prNumber": pr_number,
    }
    try:
        pr = get_pull_request(token, owner, repo, pr_number)
    except WorkflowApiError as exc:
        result["reason"] = f"pr_fetch_failed: {exc}"
        return result
    if pr.get("state") != "closed" or pr.get("merged"):
        result["reason"] = "pr_not_closed_unmerged"
        return result
    head = pr.get("head") or {}
    head_repo = (head.get("repo") or {}).get("full_name", "")
    if head_repo != f"{owner}/{repo}":
        result["reason"] = f"head_repo_mismatch: {head_repo!r}"
        return result
    branch = head.get("ref", "")
    match = SUBMISSION_BRANCH_RE.match(branch)
    if not match:
        result["reason"] = f"branch_not_submission_format: {branch!r}"
        return result
    mascot_id, version = match.group(1), match.group(2) + "." + match.group(3) + "." + match.group(4)
    if match.group(5):
        version += match.group(5)
    if match.group(6):
        version += match.group(6)
    head_sha = head.get("sha", "")

    changed_files_count = pr.get("changed_files")
    files = get_pr_files(
        token, owner, repo, pr_number,
        expected_count=changed_files_count if isinstance(changed_files_count, int) else None,
    )
    paths = [entry.get("filename", "") for entry in files]
    if not paths:
        result["reason"] = "no_changed_files"
        return result
    if any(MANIFEST_PATH_RE.fullmatch(path) is None for path in paths):
        result["reason"] = f"changed_files_outside_allowlist: {paths!r}"
        return result
    expected_path = f"mascots/{mascot_id}/manifest.json"
    if expected_path not in paths:
        result["reason"] = f"matching_manifest_missing: {paths!r}"
        return result

    manifest_entry = get_file_at_ref(token, owner, repo, expected_path, head_sha)
    if manifest_entry is None:
        result["reason"] = "manifest_unreadable"
        return result
    try:
        manifest = json.loads(decode_contents(manifest_entry))
    except (ValueError, TypeError, json.JSONDecodeError):
        result["reason"] = "manifest_invalid_json"
        return result
    if manifest.get("id") != mascot_id or manifest.get("version") != version:
        result["reason"] = "manifest_identity_mismatch"
        return result
    meta = manifest.get("release") or {}
    release_id = meta.get("releaseId")
    tag = meta.get("tag")
    expected_tag = f"draft/{mascot_id}-{version}"
    if not release_id or not tag or tag != expected_tag:
        result["reason"] = f"release_metadata_mismatch: {meta!r}"
        return result

    release = get_release_by_id(token, owner, repo, int(release_id))
    if release is None:
        result["verified"] = True
        result["reason"] = "release_already_absent"
        result["deletedReleaseId"] = release_id
        return result
    if release.get("draft") is not True:
        result["reason"] = "release_not_draft"
        return result
    if release.get("tag_name") != expected_tag:
        result["reason"] = f"release_tag_mismatch: {release.get('tag_name')!r}"
        return result

    delete_release(token, owner, repo, int(release_id))
    result["verified"] = True
    result["deletedReleaseId"] = release_id
    result["branch"] = branch
    return result


def verify_pr_manifest_and_download_asset(token: str, owner: str, repo: str,
                                          pr_number: int,
                                          dest_dir: Path) -> dict:
    """Validate a submission PR strictly, then download its draft asset.

    Everything is fetched through the GitHub API: changed files, the manifest
    at the PR head SHA, the release, and the asset. No PR-controlled code is
    executed and no user-controlled value is used as a shell argument.
    """
    pr = get_pull_request(token, owner, repo, pr_number)
    if pr.get("state") != "open":
        raise WorkflowApiError(
            f"PR {pr_number} is not open (state={pr.get('state')!r})"
        )
    head = pr.get("head") or {}
    head_repo = (head.get("repo") or {}).get("full_name", "")
    if head_repo != f"{owner}/{repo}":
        raise WorkflowApiError(
            f"PR head repository {head_repo!r} is not {owner}/{repo}"
        )
    branch = head.get("ref", "")
    match = SUBMISSION_BRANCH_RE.match(branch)
    if not match:
        raise WorkflowApiError(
            f"PR head branch {branch!r} is not a server submission branch"
        )
    mascot_id = match.group(1)
    version = match.group(2) + "." + match.group(3) + "." + match.group(4)
    if match.group(5):
        version += match.group(5)
    if match.group(6):
        version += match.group(6)
    head_sha = head.get("sha", "")

    files = get_pr_files(token, owner, repo, pr_number)
    paths = [entry.get("filename", "") for entry in files]
    expected_path = f"mascots/{mascot_id}/manifest.json"
    if paths != [expected_path]:
        raise WorkflowApiError(
            f"changed files are not exactly [{expected_path!r}]: {paths!r}"
        )

    manifest_entry = get_file_at_ref(token, owner, repo, expected_path, head_sha)
    if manifest_entry is None:
        raise WorkflowApiError("manifest not found at PR head SHA")
    try:
        manifest = json.loads(decode_contents(manifest_entry))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise WorkflowApiError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise WorkflowApiError("manifest must be a JSON object")

    expected_tag = f"draft/{mascot_id}-{version}"
    if manifest.get("id") != mascot_id or manifest.get("version") != version:
        raise WorkflowApiError("manifest id/version does not match the branch")
    submission_id = manifest.get("submissionId", "")
    if not isinstance(submission_id, str) or not SUBMISSION_ID_RE.match(submission_id):
        raise WorkflowApiError("manifest submissionId is missing or invalid")
    meta = manifest.get("release") or {}
    release_id = meta.get("releaseId")
    asset_id = meta.get("assetId")
    if not isinstance(release_id, int) or not isinstance(asset_id, int):
        raise WorkflowApiError("manifest release.releaseId/assetId must be integers")
    if meta.get("tag") != expected_tag:
        raise WorkflowApiError(
            f"manifest release.tag {meta.get('tag')!r} does not match {expected_tag!r}"
        )
    package = manifest.get("package") or {}
    file_name = package.get("fileName", "")
    sha256 = package.get("sha256", "")
    size = package.get("size", 0)
    if not isinstance(file_name, str) or not FILE_NAME_RE.match(file_name):
        raise WorkflowApiError("manifest package.fileName is invalid")
    if not isinstance(sha256, str) or not SHA256_RE.match(sha256):
        raise WorkflowApiError("manifest package.sha256 is invalid")
    if not isinstance(size, int) or size < 1:
        raise WorkflowApiError("manifest package.size is invalid")
    package_url = package.get("url", "")
    if not isinstance(package_url, str) or not package_url.startswith(
        "https://github.com/"
    ) or "/releases/download/" not in package_url:
        raise WorkflowApiError("manifest package.url is not a GitHub release URL")

    release_status, release = get_release_by_id_traced(
        token, owner, repo, release_id
    )
    if release_status != 200 or release is None:
        raise WorkflowApiError(
            f"release {release_id} referenced by the manifest does not exist "
            f"(HTTP {release_status})",
            release_status,
        )
    if release.get("draft") is not True:
        raise WorkflowApiError(
            f"release {release_id} must be a draft for PR validation "
            f"(draft={release.get('draft')!r})"
        )
    if release.get("tag_name") != expected_tag:
        raise WorkflowApiError(
            f"release {release_id} tag {release.get('tag_name')!r} does not "
            f"match {expected_tag!r}"
        )

    assets_status, assets = get_release_assets_traced(
        token, owner, repo, release_id
    )
    if assets_status != 200:
        raise WorkflowApiError(
            f"failed to list assets for release {release_id} "
            f"(HTTP {assets_status})",
            assets_status,
        )
    asset = next(
        (item for item in assets if item.get("id") == asset_id), None
    )
    if asset is None:
        raise WorkflowApiError(
            f"release {release_id} has no asset with id {asset_id}"
        )
    if asset.get("name") != file_name:
        raise WorkflowApiError(
            f"asset {asset_id} name {asset.get('name')!r} does not match "
            f"{file_name!r}"
        )
    if asset.get("state") != "uploaded":
        raise WorkflowApiError(
            f"asset {asset_id} is not uploaded (state={asset.get('state')!r})"
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"package-{pr_number}.mascot"
    try:
        download_trace = download_release_asset(
            token, owner, repo, asset_id, dest, sha256
        )
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return {
        "mascotId": mascot_id,
        "version": version,
        "submissionId": submission_id,
        "releaseId": release_id,
        "assetId": asset_id,
        "tag": expected_tag,
        "manifest": manifest,
        "assetPath": dest,
        "apiTrace": {
            "releaseHttpStatus": release_status,
            "releaseDraft": bool(release.get("draft")),
            "assetListHttpStatus": assets_status,
            "assetCount": len(assets),
            "assetDownloadHttpStatus": download_trace["apiStatus"],
            "downloadFinalStatus": download_trace["finalStatus"],
            "downloadFinalHost": download_trace["finalHost"],
            "authorizationForwardedToFinalHost": download_trace[
                "authorizationForwardedToFinalHost"
            ],
            "downloadSize": dest.stat().st_size,
            "downloadSha256": sha256,
            "redirectHops": download_trace["hops"],
        },
    }


def cli_validate(binary: Path, package_path: Path) -> tuple[bool, dict]:
    result = subprocess.run(
        [str(binary), "--json", "--mascot", "validate", str(package_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        report = {
            "ok": False,
            "errors": [f"validator returned malformed JSON (exit {result.returncode})"],
        }
    return bool(report.get("ok")), report


def main_cli_validate() -> int:
    binary = Path(sys.argv[1])
    package = Path(sys.argv[2])
    ok, report = cli_validate(binary, package)
    print(json.dumps(report, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main_cli_validate())
