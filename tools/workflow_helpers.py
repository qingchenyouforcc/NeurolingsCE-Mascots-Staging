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
    hasher = hashlib.sha256()
    written = 0
    with urllib.request.urlopen(request, timeout=120) as response, dest.open("wb") as out:
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


def github_request(method: str, url: str, token: str, payload: dict | None = None) -> dict:
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
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read()
            if not body:
                return {}
            return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"GitHub API {exc.code} for {url}: {detail}") from exc


def publish_release(token: str, owner: str, repo: str, release_id: int) -> dict:
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/{release_id}"
    return github_request("PATCH", url, token, {"draft": False})


def delete_release(token: str, owner: str, repo: str, release_id: int) -> None:
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/{release_id}"
    github_request("DELETE", url, token)


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
