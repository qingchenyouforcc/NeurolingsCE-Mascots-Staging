"""Staging end-to-end driver for the mascot submission loop.

Real mode records the three GitHub API calls required by the staging gate:
  GET /repos/{owner}/{repo}/releases/{release_id}
  GET /repos/{owner}/{repo}/releases/{release_id}/assets
  GET /repos/{owner}/{repo}/releases/assets/{asset_id}

Usage:
    STAGING_ENV=staging \
    STAGING_SERVICE_URL=https://staging.example.com \
    STAGING_GITHUB_TOKEN=<test-user token> \
    python tools/staging_e2e.py \
      --package /path/valid.mascot \
      --metadata /path/metadata.json \
      --owner <owner> --repo <repo> \
      --report staging-report.json

Token handling:
  * The GitHub user token is sent only to the submission service auth
    endpoint and to api.github.com (first host of each asset download).
  * On cross-host redirects the Authorization header is never forwarded;
    redirects are followed manually, HTTPS-only, with a redirect limit.
  * Tokens are never printed or written to the report.

Use --dry-run to print the plan without any network call.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_POLL_TIMEOUT_SECONDS = 600
DEFAULT_MAX_REDIRECTS = 5
MAX_ASSET_PAGE = 10


class StagingError(RuntimeError):
    pass


def api_json_request(method: str, url: str, token: str,
                     payload: dict | None = None) -> tuple[int, dict]:
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "neurolingsce-staging-e2e",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            body = response.read()
            try:
                return response.status, json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                return response.status, {"raw": body.decode("utf-8", "replace")}
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return exc.code, {"raw": body.decode("utf-8", "replace")}


def follow_asset_download(url: str, token: str,
                          max_redirects: int = DEFAULT_MAX_REDIRECTS
                          ) -> tuple[int, bytes, list[str]]:
    """Download a release asset with explicit redirect handling.

    Authorization is sent only on the first (GitHub API) host. Redirects are
    followed manually, HTTPS-only, with a loop and hop limit. Returns
    (status, body, hops) where hops[0] is the requested URL.
    """
    current = url
    seen: set[str] = set()
    hops: list[str] = []
    for _ in range(max_redirects + 1):
        if current in seen:
            raise StagingError(f"redirect loop detected at {current}")
        seen.add(current)
        parsed = urllib.parse.urlparse(current)
        if parsed.scheme != "https":
            raise StagingError(
                f"redirect to non-HTTPS host rejected: {current}"
            )
        hops.append(current)
        connection = http.client.HTTPSConnection(parsed.netloc, timeout=120)
        headers = {
            "User-Agent": "neurolingsce-staging-e2e",
            "Accept": "application/octet-stream",
        }
        if len(hops) == 1:
            headers["Authorization"] = f"Bearer {token}"
        path = parsed.path
        if parsed.query:
            path = f"{path}?{parsed.query}"
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            body = response.read()
            status = response.status
            location = response.getheader("Location", "")
        finally:
            connection.close()
        if status in (301, 302, 303, 307, 308):
            if not location:
                raise StagingError(f"redirect without Location at {current}")
            current = urllib.parse.urljoin(current, location)
            continue
        return status, body, hops
    raise StagingError(f"too many redirects (> {max_redirects})")


def multipart_body(boundary: str, metadata: dict, file_path: Path) -> bytes:
    chunks = []
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(b'Content-Disposition: form-data; name="metadata"\r\n')
    chunks.append(b"Content-Type: application/json\r\n\r\n")
    chunks.append(json.dumps(metadata).encode("utf-8"))
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(
        f'Content-Disposition: form-data; name="file"; '
        f'filename="{file_path.name}"\r\n'.encode()
    )
    chunks.append(b"Content-Type: application/octet-stream\r\n\r\n")
    chunks.append(file_path.read_bytes())
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def list_all_assets(api_base: str, owner: str, repo: str, release_id: int,
                    token: str) -> tuple[int, list[dict], list[str]]:
    """Fetch all asset pages; returns (status, assets, page_urls)."""
    url = (
        f"{api_base}/repos/{owner}/{repo}/releases/{release_id}/assets"
        f"?per_page=100"
    )
    assets: list[dict] = []
    page_urls: list[str] = []
    for _ in range(MAX_ASSET_PAGE):
        status, payload = api_json_request("GET", url, token)
        page_urls.append(url)
        if status != 200:
            return status, assets, page_urls
        if not isinstance(payload, list):
            return status, assets, page_urls
        assets.extend(payload)
        link = None
        # The Link header is not exposed by api_json_request; redo with
        # urllib to capture it when the payload looks paginated.
        if len(payload) == 100:
            link = _link_header(url, token)
        if not link:
            return 200, assets, page_urls
        url = link
    return 200, assets, page_urls


def _link_header(url: str, token: str) -> str | None:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "neurolingsce-staging-e2e",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            headers = dict(response.getheaders())
            body = response.read()
            link = headers.get("Link", "")
            for part in link.split(","):
                section = part.split(";")
                if len(section) >= 2 and 'rel="next"' in section[1]:
                    return section[0].strip().strip("<>")
            return None
    except urllib.error.HTTPError:
        return None


def get_check_runs(api_base: str, owner: str, repo: str, head_sha: str,
                   token: str) -> tuple[int, dict]:
    url = (
        f"{api_base}/repos/{owner}/{repo}/commits/{head_sha}/check-runs"
        f"?per_page=100"
    )
    return api_json_request("GET", url, token)


def get_actions_runs(api_base: str, owner: str, repo: str, head_sha: str,
                     token: str) -> tuple[int, dict]:
    url = (
        f"{api_base}/repos/{owner}/{repo}/actions/runs"
        f"?head_sha={head_sha}&per_page=20"
    )
    return api_json_request("GET", url, token)


def poll_submission(service_url: str, submission_id: str,
                    session_token: str, timeout_seconds: int) -> dict:
    deadline = time.monotonic() + timeout_seconds
    url = f"{service_url}/v1/submissions/{submission_id}"
    while time.monotonic() < deadline:
        status, payload = api_json_request("GET", url, session_token)
        if status != 200:
            raise StagingError(
                f"GET /v1/submissions/{submission_id} returned {status}"
            )
        if payload.get("status") in ("pending", "failed"):
            return payload
        time.sleep(10)
    raise StagingError("submission did not reach a terminal state in time")


def run_dry_run(args, service_url: str, github_token: str) -> int:
    print("[dry-run] no network calls were made")
    print(f"[dry-run] service_url={service_url or '<unset>'}")
    print(f"[dry-run] github_token={'set' if github_token else '<unset>'}")
    print(f"[dry-run] package={args.package} sha256={sha256_of(Path(args.package))}")
    steps = [
        "1. POST /v1/auth/github (Bearer test-user token) -> session token",
        "2. POST /v1/submissions (multipart, idempotency key) -> 201",
        "3. poll GET /v1/submissions/<id> until pending/failed",
        "4. GET /repos/<owner>/<repo>/releases/<release_id> (record status/draft)",
        "5. GET /repos/<owner>/<repo>/releases/<release_id>/assets (Link pagination)",
        "6. GET /repos/<owner>/<repo>/releases/assets/<asset_id> "
        "(200/302, final host, Authorization never forwarded, SHA-256)",
        "7. GET commits/<head_sha>/check-runs + actions/runs (PR check result)",
        "8. write JSON report (no tokens)",
    ]
    for step in steps:
        print(f"[dry-run] {step}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, help="path to a .mascot")
    parser.add_argument("--metadata", help="path to JSON metadata (required "
                                           "for real mode)")
    parser.add_argument("--owner", help="staging registry owner")
    parser.add_argument("--repo", help="staging registry repo")
    parser.add_argument("--report", help="write JSON report to this path")
    parser.add_argument("--service-url", help="submission service base URL")
    parser.add_argument("--api-base", default="https://api.github.com")
    parser.add_argument("--max-redirects", type=int,
                        default=DEFAULT_MAX_REDIRECTS)
    parser.add_argument("--dry-run", action="store_true",
                        help="print intended steps without side effects")
    parser.add_argument("--poll-timeout", type=int,
                        default=DEFAULT_POLL_TIMEOUT_SECONDS)
    args = parser.parse_args()

    service_url = (args.service_url or
                   os.environ.get("STAGING_SERVICE_URL", "")).rstrip("/")
    github_token = os.environ.get("STAGING_GITHUB_TOKEN", "")
    owner = args.owner or os.environ.get("STAGING_OWNER", "")
    repo = args.repo or os.environ.get("STAGING_REPO", "")
    package = Path(args.package)
    if not package.is_file():
        print(f"package not found: {package}", file=sys.stderr)
        return 2

    if args.dry_run:
        return run_dry_run(args, service_url, github_token)

    if os.environ.get("STAGING_ENV", "") != "staging":
        print("STAGING_ENV=staging is required for real execution",
              file=sys.stderr)
        return 2
    if not service_url or not github_token or not owner or not repo:
        print("STAGING_SERVICE_URL, STAGING_GITHUB_TOKEN, --owner and "
              "--repo are required", file=sys.stderr)
        return 2
    if not args.metadata:
        print("--metadata is required for real execution", file=sys.stderr)
        return 2
    metadata_path = Path(args.metadata)
    if not metadata_path.is_file():
        print(f"metadata not found: {metadata_path}", file=sys.stderr)
        return 2
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    report: dict = {
        "mode": "real",
        "owner": owner,
        "repo": repo,
        "packageSha256": sha256_of(package),
        "apiCalls": [],
        "release": {},
        "assetDownload": {},
        "prCheck": {},
    }

    auth_status, auth_payload = api_json_request(
        "POST", f"{service_url}/v1/auth/github", github_token
    )
    report["apiCalls"].append(
        {"method": "POST", "url": f"{service_url}/v1/auth/github",
         "status": auth_status}
    )
    if auth_status != 200:
        print(f"auth failed: {auth_status}", file=sys.stderr)
        report["result"] = "auth_failed"
        _write_report(args.report, report)
        return 3
    session_token = auth_payload["token"]

    boundary = f"staging-boundary-{int(time.time() * 1000)}"
    key_hash = hashlib.sha256(
        json.dumps(metadata, sort_keys=True).encode("utf-8")
        + package.read_bytes()
    ).hexdigest()
    # Multipart upload needs a raw body; api_json_request cannot encode it.
    submit_status, submit_payload = _multipart_submit(
        service_url, session_token, boundary, key_hash, metadata, package
    )
    report["apiCalls"].append(
        {"method": "POST", "url": f"{service_url}/v1/submissions",
         "status": submit_status}
    )
    if submit_status not in (200, 201):
        print(f"submission failed: {submit_status} {submit_payload}",
              file=sys.stderr)
        report["result"] = "submission_failed"
        report["submissionError"] = submit_payload
        _write_report(args.report, report)
        return 3
    submission_id = submit_payload["id"]
    report["submissionId"] = submission_id
    report["prNumber"] = submit_payload.get("pr", {}).get("number")
    expected_sha256 = (
        submit_payload.get("package", {}).get("sha256")
        or metadata.get("package", {}).get("sha256", "")
    )
    release = submit_payload.get("release", {})
    release_id = release.get("releaseId")
    asset_id = release.get("assetId")
    report["releaseId"] = release_id
    report["assetId"] = asset_id

    final = poll_submission(service_url, submission_id, session_token,
                            args.poll_timeout)
    report["submissionStatus"] = final.get("status")
    if final.get("status") == "failed":
        report["result"] = "submission_failed"
        report["submissionError"] = final.get("error")
        _write_report(args.report, report)
        return 3

    release_status, release_view = api_json_request(
        "GET", f"{args.api_base}/repos/{owner}/{repo}/releases/{release_id}",
        github_token,
    )
    report["apiCalls"].append(
        {"method": "GET", "url": (
            f"{args.api_base}/repos/{owner}/{repo}/releases/{release_id}"),
         "status": release_status}
    )
    report["release"] = {
        "httpStatus": release_status,
        "draft": release_view.get("draft") if isinstance(release_view, dict)
        else None,
    }

    assets_status, assets, asset_pages = list_all_assets(
        args.api_base, owner, repo, release_id, github_token
    )
    report["apiCalls"].append(
        {"method": "GET", "url": (
            f"{args.api_base}/repos/{owner}/{repo}/releases/{release_id}/assets"
            f"?per_page=100"),
         "status": assets_status, "pages": asset_pages}
    )
    report["release"]["assetsStatus"] = assets_status
    report["release"]["assetCount"] = len(assets)

    asset_url = (
        f"{args.api_base}/repos/{owner}/{repo}/releases/assets/{asset_id}"
    )
    try:
        asset_status, asset_body, hops = follow_asset_download(
            asset_url, github_token, args.max_redirects
        )
    except StagingError as exc:
        report["assetDownload"] = {"error": str(exc), "hops": []}
        print(f"asset download failed: {exc}", file=sys.stderr)
        report["result"] = "asset_download_failed"
        _write_report(args.report, report)
        return 3
    report["apiCalls"].append(
        {"method": "GET", "url": asset_url, "status": asset_status}
    )
    final_host = urllib.parse.urlparse(hops[-1]).netloc if hops else ""
    report["assetDownload"] = {
        "httpStatus": asset_status,
        "hops": hops,
        "finalHost": final_host,
        "authorizationForwarded": False,
        "downloadSha256": hashlib.sha256(asset_body).hexdigest(),
        "expectedSha256": expected_sha256,
    }
    if hashlib.sha256(asset_body).hexdigest() != expected_sha256:
        report["result"] = "sha256_mismatch"
        _write_report(args.report, report)
        return 3

    pr_number = submit_payload.get("pr", {}).get("number")
    if pr_number:
        pr_status, pr_view = api_json_request(
            "GET", f"{args.api_base}/repos/{owner}/{repo}/pulls/{pr_number}",
            github_token,
        )
        head_sha = pr_view.get("head", {}).get("sha", "") if isinstance(
            pr_view, dict
        ) else ""
        if head_sha:
            check_status, check_view = get_check_runs(
                args.api_base, owner, repo, head_sha, github_token
            )
            runs_status, runs_view = get_actions_runs(
                args.api_base, owner, repo, head_sha, github_token
            )
            report["apiCalls"].append(
                {"method": "GET", "url": (
                    f"{args.api_base}/repos/{owner}/{repo}/commits/"
                    f"{head_sha}/check-runs"), "status": check_status}
            )
            report["prCheck"] = {
                "headSha": head_sha,
                "checksHttpStatus": check_status,
                "checkRuns": [
                    {
                        "name": run.get("name"),
                        "status": run.get("status"),
                        "conclusion": run.get("conclusion"),
                    }
                    for run in check_view.get("check_runs", [])
                    if isinstance(run, dict)
                ],
                "workflowRuns": [
                    {
                        "id": run.get("id"),
                        "event": run.get("event"),
                        "conclusion": run.get("conclusion"),
                    }
                    for run in runs_view.get("workflow_runs", [])
                    if isinstance(run, dict)
                ],
            }

    report["result"] = "pending_requires_merge_and_publish"
    _write_report(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    print("\nStaging driver finished the pre-merge gate. Next manual steps: "
          "merge PR, observe publish-and-deploy, verify Pages and client "
          "install (see docs/STAGING_E2E.md).")
    return 0


def _multipart_submit(service_url: str, session_token: str, boundary: str,
                      key_hash: str, metadata: dict,
                      package: Path) -> tuple[int, dict]:
    body = multipart_body(boundary, metadata, package)
    req = urllib.request.Request(
        f"{service_url}/v1/submissions", data=body, method="POST",
        headers={
            "Authorization": f"Bearer {session_token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "X-Idempotency-Key": key_hash,
            "Accept": "application/json",
            "User-Agent": "neurolingsce-staging-e2e",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body_text)
        except json.JSONDecodeError:
            return exc.code, {"raw": body_text}


def _write_report(path: str | None, report: dict) -> None:
    if path:
        Path(path).write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )


if __name__ == "__main__":
    sys.exit(main())
