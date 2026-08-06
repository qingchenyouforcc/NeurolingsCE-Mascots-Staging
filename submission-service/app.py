"""NeurolingsCE mascot submission service (stdlib-first)."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from github_client import GitHubApiError, GitHubClient  # noqa: E402
from multipart import MultipartError, parse_multipart  # noqa: E402
from package_checks import validate_mascot  # noqa: E402
from redact import redact_headers, redact_text  # noqa: E402

ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$"
)
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
FILE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.mascot$")


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(str(record.msg))
        return True


LOGGER = logging.getLogger("submission-service")
LOGGER.addFilter(RedactingFilter())


class Config:
    def __init__(self) -> None:
        self.port = int(os.environ.get("SUBMISSION_PORT", "8000"))
        self.storage_dir = Path(os.environ.get("SUBMISSION_STORAGE_DIR", "data"))
        self.base_url = os.environ.get("SUBMISSION_BASE_URL", "http://localhost:8000")
        self.app_id = os.environ.get("GITHUB_APP_ID", "")
        self.installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID", "")
        self.private_key_path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH", "")
        self.owner = os.environ.get("GITHUB_OWNER", "qingchenyouforcc")
        self.repo = os.environ.get("GITHUB_REPO", "NeurolingsCE-Mascots")
        self.max_upload_bytes = int(os.environ.get("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))
        self.rate_limit_per_minute = int(
            os.environ.get("RATE_LIMIT_SUBMISSIONS_PER_MINUTE", "10")
        )
        self.service_token = os.environ.get("SUBMISSION_SERVICE_TOKEN", "")
        self.validator_cli = os.environ.get("VALIDATOR_CLI", "")

    @property
    def github_configured(self) -> bool:
        return bool(self.app_id and self.installation_id and self.private_key_path)

    def private_key_pem(self) -> str:
        return Path(self.private_key_path).read_text(encoding="utf-8")


class SubmissionStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, submission_id: str) -> Path:
        return self.root / f"{submission_id}.json"

    def save(self, submission: dict) -> None:
        with self._lock:
            path = self._path(submission["id"])
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(submission, indent=2, sort_keys=True), encoding="utf-8"
            )
            tmp.replace(path)

    def load(self, submission_id: str) -> dict | None:
        path = self._path(submission_id)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def exists(self, submission_id: str) -> bool:
        return self._path(submission_id).is_file()


class RateLimiter:
    def __init__(self, limit_per_minute: int) -> None:
        self.limit = limit_per_minute
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            events = self._events.setdefault(key, deque())
            while events and events[0] <= now - 60.0:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True


class SubmissionService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = SubmissionStore(config.storage_dir)
        self.rate_limiter = RateLimiter(config.rate_limit_per_minute)
        self.github = GitHubClient(config.owner, config.repo)

    def _installation_token(self) -> str:
        if not self.config.github_configured:
            raise GitHubApiError(
                "github_unconfigured",
                "the submission service is not configured with a GitHub App",
                503,
            )
        return self.github.get_installation_token(
            self.config.app_id,
            self.config.installation_id,
            self.config.private_key_pem(),
        )

    def _authorize_owner(self, submission: dict, authorization: str) -> bool:
        if self.config.service_token and authorization == f"Bearer {self.config.service_token}":
            return True
        token = authorization.removeprefix("Bearer ").strip()
        if not token:
            return False
        try:
            user = self.github.get_user(token)
        except GitHubApiError:
            return False
        return user.get("login") == submission.get("owner")

    def create_submission(self, parsed, client_address: str,
                          idempotency_key: str) -> tuple[dict, bool]:
        if not self.rate_limiter.allow(f"ip:{client_address}"):
            raise ServiceError("rate_limited", "submission rate limit exceeded", 429)
        if idempotency_key:
            key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
            existing = self._find_by_idempotency(key_hash)
            if existing is not None:
                return existing, True
        metadata_text = parsed.fields.get("metadata", "")
        if not metadata_text:
            raise ServiceError("invalid_metadata", "metadata field is required", 400)
        try:
            metadata = json.loads(metadata_text)
        except json.JSONDecodeError as exc:
            raise ServiceError("invalid_metadata", f"metadata is not valid JSON: {exc.msg}", 400) from exc
        if not isinstance(metadata, dict):
            raise ServiceError("invalid_metadata", "metadata must be a JSON object", 400)
        uploaded = parsed.files.get("file")
        if uploaded is None:
            raise ServiceError("invalid_upload", "file field is required", 400)
        return self._process_submission(metadata, uploaded, idempotency_key), False

    def _find_by_idempotency(self, key_hash: str) -> dict | None:
        for path in self.config.storage_dir.glob("*.json"):
            try:
                submission = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if submission.get("idempotencyKeyHash") == key_hash:
                return submission
        return None

    def _process_submission(self, metadata: dict, uploaded, idempotency_key: str) -> dict:
        submission_id = uuid.uuid4().hex[:24]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        mid = metadata.get("id")
        version = metadata.get("version")
        if not isinstance(mid, str) or not ID_RE.match(mid) or len(mid) > 64:
            raise ServiceError("invalid_metadata", "id is invalid", 422)
        if not isinstance(version, str) or not SEMVER_RE.match(version):
            raise ServiceError("invalid_metadata", "version must be valid SemVer", 422)
        name = metadata.get("name")
        summary = metadata.get("summary")
        description = metadata.get("description")
        license_id = metadata.get("license")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ServiceError("invalid_metadata", "name is invalid", 422)
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 300:
            raise ServiceError("invalid_metadata", "summary is invalid", 422)
        if not isinstance(description, str) or not description.strip() or len(description) > 20000:
            raise ServiceError("invalid_metadata", "description is invalid", 422)
        if not isinstance(license_id, str) or not re.match(r"^[A-Za-z0-9.+-]+$", license_id):
            raise ServiceError("invalid_metadata", "license is invalid", 422)
        token = metadata.get("githubToken")
        if not isinstance(token, str) or not token:
            raise ServiceError("auth_required", "githubToken is required", 401)
        try:
            user = self.github.get_user(token)
        except GitHubApiError as exc:
            raise ServiceError("auth_failed", "GitHub identity verification failed", 401) from exc
        login = user.get("login", "")
        if not login:
            raise ServiceError("auth_failed", "GitHub identity has no login", 401)
        if not self.rate_limiter.allow(f"user:{login}"):
            raise ServiceError("rate_limited", "submission rate limit exceeded", 429)

        authors = metadata.get("authors")
        maintainers = metadata.get("maintainers")
        if not isinstance(authors, list) or not authors:
            raise ServiceError("invalid_metadata", "authors must be a non-empty array", 422)
        if not isinstance(maintainers, list) or not maintainers:
            raise ServiceError("invalid_metadata", "maintainers must be a non-empty array", 422)
        if any(not isinstance(login, str) or not LOGIN_RE.match(login) for login in maintainers):
            raise ServiceError("invalid_metadata", "maintainers contain an invalid login", 422)
        first_author = authors[0]
        if not isinstance(first_author, dict) or first_author.get("githubLogin") != login:
            raise ServiceError(
                "identity_mismatch",
                "authors[0].githubLogin must match the authenticated GitHub user",
                403,
            )
        if login not in maintainers:
            raise ServiceError(
                "identity_mismatch",
                "the authenticated user must be listed in maintainers",
                403,
            )

        package_ok, package_errors = validate_mascot(
            uploaded.temp_path, self.config.validator_cli
        )
        if not package_ok:
            raise ServiceError("package_invalid", "package failed validation", 422, package_errors)
        if not FILE_NAME_RE.match(uploaded.file_name or ""):
            raise ServiceError(
                "invalid_metadata",
                "file name must match ^[A-Za-z0-9._-]+\\.mascot$",
                422,
            )

        sha256 = hashlib.sha256(uploaded.temp_path.read_bytes()).hexdigest()
        token_app = self._installation_token()
        existing = self.github.get_file(token_app, f"mascots/{mid}/manifest.json")
        if existing is not None:
            try:
                existing_text = base64.b64decode(
                    existing.get("content", "")
                ).decode("utf-8")
                existing_manifest = json.loads(existing_text)
            except (KeyError, json.JSONDecodeError):
                raise ServiceError("registry_unreadable", "existing manifest is unreadable", 500) from None
            if existing_manifest.get("version") == version:
                raise ServiceError(
                    "duplicate_id_version",
                    f"id {mid!r} version {version!r} already exists",
                    409,
                )

        tag = f"draft/{mid}-{version}"
        release = self.github.create_draft_release(
            token_app,
            tag,
            f"Mascot {mid} v{version}",
            f"Submission by @{login}: {metadata.get('summary', '')}",
        )
        release_id = release.get("id")
        asset = self.github.upload_release_asset(
            token_app, release_id, uploaded.temp_path, uploaded.file_name
        )
        asset_id = asset.get("id")
        asset_url = asset.get("browser_download_url", "")

        base_sha = self.github.get_branch_head_sha(token_app)
        branch = f"submission/{mid}-{version}"
        self.github.create_branch(token_app, branch, base_sha)
        manifest = {
            "schemaVersion": "1",
            "id": mid,
            "name": metadata.get("name", mid),
            "version": version,
            "summary": metadata.get("summary", ""),
            "description": metadata.get("description", ""),
            "authors": authors,
            "maintainers": maintainers,
            "license": metadata.get("license", ""),
            "isDerivative": bool(metadata.get("isDerivative", False)),
            "upstream": metadata.get("upstream", ""),
            "minimumNeurolingsCEVersion": metadata.get("minimumNeurolingsCEVersion", "0.5.1"),
            "package": {
                "fileName": uploaded.file_name,
                "url": asset_url,
                "size": uploaded.size,
                "sha256": sha256,
                "contentType": uploaded.content_type,
            },
            "tags": metadata.get("tags", []),
            "categories": metadata.get("categories", []),
            "status": "draft",
            "createdAt": now,
            "updatedAt": now,
            "release": {"releaseId": release_id, "assetId": asset_id, "tag": tag},
        }
        self.github.create_or_update_file(
            token_app,
            f"mascots/{mid}/manifest.json",
            f"Add {mid} v{version} via submission {submission_id}",
            json.dumps(manifest, indent=2, sort_keys=True),
            branch,
        )
        pr = self.github.create_pull_request(
            token_app,
            f"Mascot: {manifest['name']} ({mid} v{version})",
            branch,
            "main",
            f"Submission `{submission_id}`\n\n{manifest['summary']}",
        )
        submission = {
            "id": submission_id,
            "status": "pending",
            "owner": login,
            "mascotId": mid,
            "version": version,
            "createdAt": now,
            "updatedAt": now,
            "package": {"fileName": uploaded.file_name, "size": uploaded.size, "sha256": sha256},
            "idempotencyKeyHash": hashlib.sha256(
                idempotency_key.encode("utf-8")
            ).hexdigest() if idempotency_key else "",
            "release": {"releaseId": release_id, "assetId": asset_id, "tag": tag},
            "pr": {"number": pr.get("number"), "url": pr.get("html_url", "")},
        }
        self.store.save(submission)
        return submission

    def get_submission(self, submission_id: str, authorization: str) -> dict:
        submission = self.store.load(submission_id)
        if submission is None:
            raise ServiceError("not_found", "submission not found", 404)
        if not self._authorize_owner(submission, authorization):
            raise ServiceError("forbidden", "not authorized to view this submission", 403)
        return submission

    def delete_submission(self, submission_id: str, authorization: str) -> dict:
        submission = self.store.load(submission_id)
        if submission is None:
            raise ServiceError("not_found", "submission not found", 404)
        if not self._authorize_owner(submission, authorization):
            raise ServiceError("forbidden", "not authorized to delete this submission", 403)
        if submission.get("status") != "cancelled" and self.config.github_configured:
            token = self._installation_token()
            release = submission.get("release") or {}
            if release.get("releaseId"):
                try:
                    self.github.delete_release(token, release["releaseId"])
                except GitHubApiError as exc:
                    if exc.status != 404:
                        raise
            pr = submission.get("pr") or {}
            if pr.get("number"):
                try:
                    self.github.close_pull_request(token, pr["number"])
                except GitHubApiError as exc:
                    if exc.status != 404:
                        raise
        submission["status"] = "cancelled"
        submission["updatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.store.save(submission)
        return submission


class ServiceError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400, details=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


class Handler(BaseHTTPRequestHandler):
    service = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args) -> None:
        LOGGER.info("http %s", redact_text(fmt % args))

    def _send(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, error: ServiceError | Exception) -> None:
        if isinstance(error, ServiceError):
            code, status, message, details = error.code, error.status, str(error), error.details
        elif isinstance(error, MultipartError):
            code, status, message, details = "bad_request", 400, str(error), None
        elif isinstance(error, GitHubApiError):
            code, status, message, details = error.code, error.status or 502, str(error), None
        else:
            code, status, message, details = "internal_error", 500, "internal error", None
            LOGGER.exception("unhandled request error")
        obj = {"error": {"code": code, "message": redact_text(message)}}
        if details:
            obj["error"]["details"] = details
        LOGGER.warning("request failed code=%s status=%s headers=%s",
                       code, status, redact_headers(dict(self.headers)))
        self._send(status, obj)

    def _read_content_length(self) -> int | None:
        value = self.headers.get("Content-Length")
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def do_POST(self) -> None:
        if self.path != "/v1/submissions":
            self._send_error(ServiceError("not_found", "unknown endpoint", 404))
            return
        parsed = None
        try:
            parsed = parse_multipart(
                self.rfile,
                self.headers.get("Content-Type", ""),
                self._read_content_length(),
                self.service.config.max_upload_bytes,
            )
            result, replayed = self.service.create_submission(
                parsed,
                self.client_address[0],
                self.headers.get("X-Idempotency-Key", ""),
            )
            status = 200 if replayed else 201
            self._send(status, {"id": result["id"], "status": result["status"], "pr": result["pr"]})
        except Exception as exc:  # noqa: BLE001
            self._send_error(exc)
        finally:
            for uploaded in getattr(parsed or {}, "files", {}).values():
                uploaded.temp_path.unlink(missing_ok=True)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, {"ok": True})
            return
        match = re.match(r"^/v1/submissions/([0-9a-f]{24})$", self.path)
        if not match:
            self._send_error(ServiceError("not_found", "unknown endpoint", 404))
            return
        try:
            result = self.service.get_submission(
                match.group(1), self.headers.get("Authorization", "")
            )
            self._send(200, result)
        except Exception as exc:  # noqa: BLE001
            self._send_error(exc)

    def do_DELETE(self) -> None:
        match = re.match(r"^/v1/submissions/([0-9a-f]{24})$", self.path)
        if not match:
            self._send_error(ServiceError("not_found", "unknown endpoint", 404))
            return
        try:
            result = self.service.delete_submission(
                match.group(1), self.headers.get("Authorization", "")
            )
            self._send(200, {"id": result["id"], "status": result["status"]})
        except Exception as exc:  # noqa: BLE001
            self._send_error(exc)


def build_service(config: Config | None = None) -> SubmissionService:
    config = config or Config()
    return SubmissionService(config)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    config = Config()
    Handler.service = build_service(config)
    server = ThreadingHTTPServer(("0.0.0.0", config.port), Handler)
    LOGGER.info(
        "submission service listening port=%s storage=%s github_configured=%s",
        config.port, config.storage_dir, config.github_configured,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
