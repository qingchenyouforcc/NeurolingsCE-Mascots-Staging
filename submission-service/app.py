"""NeurolingsCE mascot submission service (stdlib-first)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from github_client import GitHubApiError, GitHubClient  # noqa: E402
from multipart import MultipartError, parse_multipart  # noqa: E402
from package_checks import (  # noqa: E402
    validate_mascot,
    validate_mascot_detailed,
    validator_self_check,
)
from redact import redact_headers, redact_text  # noqa: E402

ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$"
)
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
USER_ID_RE = re.compile(r"^[0-9]{1,20}$")
FILE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.mascot$")
CONTENT_TYPE_RE = re.compile(r"^[A-Za-z0-9._+/-]+$")
MANIFEST_PATH_RE = re.compile(
    r"^mascots/([a-z0-9]+(?:-[a-z0-9]+)*)/manifest\.json$"
)

MAX_ID_LENGTH = 64
MAX_VERSION_LENGTH = 128
MAX_BRANCH_LENGTH = 200
MAX_TITLE_LENGTH = 256
MAX_PR_BODY_LENGTH = 4096
RESERVED_IDS = {
    ".github",
    "mascots",
    "docs",
    "tools",
    "schemas",
    "generated",
    "submission-service",
    "examples",
    "con",
    "prn",
    "aux",
    "nul",
    "com1",
    "com2",
    "com3",
    "com4",
    "com5",
    "com6",
    "com7",
    "com8",
    "com9",
    "lpt1",
    "lpt2",
    "lpt3",
    "lpt4",
    "lpt5",
    "lpt6",
    "lpt7",
    "lpt8",
    "lpt9",
}
ALLOWED_ENVS = {"development", "test", "production"}
SESSION_AUDIENCE = "neurolingsce-submission"
SESSION_ISSUER = "neurolingsce-submission-service"
SESSION_TTL_MIN = 300
SESSION_TTL_MAX = 600
SESSION_ALG = "HS256"
SESSION_SKEW_SECONDS = 60
SESSION_SECRET_MIN_BYTES = 32


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(str(record.msg))
        return True


LOGGER = logging.getLogger("submission-service")
LOGGER.addFilter(RedactingFilter())


def _clean_text(value: str, max_length: int, keep_newlines: bool = False) -> str:
    if keep_newlines:
        cleaned = "".join(
            char if char.isprintable() or char == "\n" else " "
            for char in value
        )
    else:
        cleaned = "".join(
            char if char.isprintable() else " " for char in value
        )
    cleaned = re.sub(r" {2,}", " ", cleaned).strip()
    return cleaned[:max_length]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def decode_session_secret(value: str) -> bytes:
    """Decode a session secret from hex or base64.

    Returns the decoded key bytes. Raises ValueError for undecodable input.
    """
    if not value:
        raise ValueError("session secret must not be empty")
    text = value.strip()
    if re.fullmatch(r"[0-9a-fA-F]{64,}", text):
        return bytes.fromhex(text)
    try:
        padded = text + "=" * (-len(text) % 4)
        decoded = base64.b64decode(padded, validate=True)
    except (ValueError, TypeError):
        decoded = b""
    if decoded:
        return decoded
    raise ValueError(
        "SUBMISSION_SESSION_SECRET must be hex (>= 64 chars) or base64 "
        "encoding at least 32 bytes"
    )


def parse_semver(version: str) -> tuple:
    """Return a comparable SemVer tuple; build metadata is ignored."""
    match = SEMVER_RE.match(version)
    if not match:
        raise ValueError(f"invalid SemVer: {version!r}")
    major, minor, patch = (int(match.group(i)) for i in (1, 2, 3))
    prerelease = match.group(5) or None
    return major, minor, patch, prerelease


def semver_gt(left: str, right: str) -> bool:
    """Strict SemVer precedence: left > right (build metadata ignored)."""
    l_major, l_minor, l_patch, l_pre = parse_semver(left)
    r_major, r_minor, r_patch, r_pre = parse_semver(right)
    if (l_major, l_minor, l_patch) != (r_major, r_minor, r_patch):
        return (l_major, l_minor, l_patch) > (r_major, r_minor, r_patch)
    if l_pre is None and r_pre is None:
        return False
    if l_pre is None:
        return True
    if r_pre is None:
        return False
    l_parts = l_pre.split(".")
    r_parts = r_pre.split(".")
    for l_item, r_item in zip(l_parts, r_parts):
        l_num = l_item.isdigit()
        r_num = r_item.isdigit()
        if l_num and r_num:
            if int(l_item) != int(r_item):
                return int(l_item) > int(r_item)
        elif l_num != r_num:
            return not l_num  # numeric identifiers have lower precedence
        elif l_item != r_item:
            return l_item > r_item
    return len(l_parts) > len(r_parts)


class Config:
    def __init__(self) -> None:
        self.port = int(os.environ.get("SUBMISSION_PORT", "8000"))
        self.storage_dir = Path(os.environ.get("SUBMISSION_STORAGE_DIR", "data"))
        self.base_url = os.environ.get("SUBMISSION_BASE_URL", "http://localhost:8000")
        self.app_id = os.environ.get("GITHUB_PUBLISHER_APP_ID", "")
        self.installation_id = os.environ.get("GITHUB_PUBLISHER_INSTALLATION_ID", "")
        self.private_key_path = os.environ.get("GITHUB_PUBLISHER_PRIVATE_KEY_PATH", "")
        self.owner = os.environ.get("GITHUB_OWNER", "qingchenyouforcc")
        self.repo = os.environ.get("GITHUB_REPO", "NeurolingsCE-Mascots")
        self.max_upload_bytes = int(os.environ.get("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))
        self.rate_limit_per_minute = int(
            os.environ.get("RATE_LIMIT_SUBMISSIONS_PER_MINUTE", "10")
        )
        self.auth_rate_limit_per_minute = int(
            os.environ.get("SUBMISSION_AUTH_RATE_LIMIT_PER_MINUTE", "30")
        )
        self.service_token = os.environ.get("SUBMISSION_SERVICE_TOKEN", "")
        self.validator_cli = os.environ.get("VALIDATOR_CLI", "")
        self.submission_env = os.environ.get(
            "SUBMISSION_ENV", "development"
        ).strip().lower()
        if self.submission_env not in ALLOWED_ENVS:
            raise ValueError(
                f"SUBMISSION_ENV must be one of {sorted(ALLOWED_ENVS)}"
            )
        self.session_secret_raw = os.environ.get("SUBMISSION_SESSION_SECRET", "")
        if self.production:
            if not self.session_secret_raw:
                raise ValueError(
                    "SUBMISSION_ENV=production requires SUBMISSION_SESSION_SECRET "
                    "to be set (>= 32 bytes after hex/base64 decoding)"
                )
            decoded = decode_session_secret(self.session_secret_raw)
            if len(decoded) < SESSION_SECRET_MIN_BYTES:
                raise ValueError(
                    "SUBMISSION_SESSION_SECRET must decode to at least "
                    f"{SESSION_SECRET_MIN_BYTES} bytes"
                )
            self.session_secret = decoded
        else:
            if not self.session_secret_raw:
                self.session_secret = secrets.token_bytes(SESSION_SECRET_MIN_BYTES)
                LOGGER.warning(
                    "WARNING: ephemeral submission session key; not suitable "
                    "for production"
                )
            else:
                self.session_secret = decode_session_secret(self.session_secret_raw)
        ttl = int(os.environ.get("SUBMISSION_SESSION_TTL_SECONDS", "600"))
        self.session_ttl_seconds = max(SESSION_TTL_MIN, min(SESSION_TTL_MAX, ttl))
        self.validator_timeout_seconds = int(
            os.environ.get("VALIDATOR_TIMEOUT_SECONDS", "120")
        )

    @property
    def github_configured(self) -> bool:
        return bool(self.app_id and self.installation_id and self.private_key_path)

    @property
    def production(self) -> bool:
        return self.submission_env == "production"

    def private_key_pem(self) -> str:
        return Path(self.private_key_path).read_text(encoding="utf-8")


def validate_production_config(config: Config) -> None:
    """Fail closed: production requires a working public validator."""
    if not config.production:
        return
    cli = config.validator_cli
    if not cli:
        raise RuntimeError(
            "SUBMISSION_ENV=production requires VALIDATOR_CLI to be configured"
        )
    cli_path = Path(cli)
    if not cli_path.is_file():
        raise RuntimeError(
            f"VALIDATOR_CLI does not exist or is not a file: {cli!r}"
        )
    if not os.access(cli_path, os.X_OK):
        raise RuntimeError(
            f"VALIDATOR_CLI is not executable: {cli!r}"
        )
    ok, detail = validator_self_check(cli, config.validator_timeout_seconds)
    if not ok:
        raise RuntimeError(f"validator self-check failed: {detail}")


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


class SessionManager:
    def __init__(self, secret: bytes, ttl_seconds: int) -> None:
        self.secret = secret
        self.ttl_seconds = ttl_seconds

    def issue(self, login: str, user_id: str) -> str:
        now = int(time.time())
        header = {"alg": SESSION_ALG, "typ": "JWT"}
        payload = {
            "sub": user_id,
            "login": login,
            "iat": now,
            "nbf": now,
            "exp": now + self.ttl_seconds,
            "aud": SESSION_AUDIENCE,
            "iss": SESSION_ISSUER,
            "jti": uuid.uuid4().hex,
        }
        header_b64 = _b64url(
            json.dumps(header, separators=(",", ":")).encode("utf-8")
        )
        body_b64 = _b64url(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
        signing_input = header_b64 + "." + body_b64
        signature = hmac.new(
            self.secret, signing_input.encode("ascii"), hashlib.sha256
        ).digest()
        return signing_input + "." + _b64url(signature)

    def verify(self, token: str) -> dict | None:
        try:
            header_b64, body_b64, signature = token.split(".")
            expected = hmac.new(
                self.secret,
                (header_b64 + "." + body_b64).encode("ascii"),
                hashlib.sha256,
            ).digest()
            actual = _b64url_decode(signature)
            if not hmac.compare_digest(expected, actual):
                return None
            header = json.loads(_b64url_decode(header_b64).decode("utf-8"))
            payload = json.loads(_b64url_decode(body_b64).decode("utf-8"))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(header, dict) or header.get("alg") != SESSION_ALG:
            return None
        if header.get("typ") != "JWT":
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("aud") != SESSION_AUDIENCE or payload.get("iss") != SESSION_ISSUER:
            return None
        now = int(time.time())
        if not isinstance(payload.get("iat"), int) or payload["iat"] > now + SESSION_SKEW_SECONDS:
            return None
        if not isinstance(payload.get("nbf"), int) or payload["nbf"] > now + SESSION_SKEW_SECONDS:
            return None
        if not isinstance(payload.get("exp"), int) or payload["exp"] < now:
            return None
        if not isinstance(payload.get("login"), str) or not isinstance(payload.get("sub"), str):
            return None
        if not isinstance(payload.get("jti"), str) or not payload["jti"]:
            return None
        return payload


class SubmissionService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = SubmissionStore(config.storage_dir)
        self.rate_limiter = RateLimiter(config.rate_limit_per_minute)
        self.auth_rate_limiter = RateLimiter(config.auth_rate_limit_per_minute)
        self.github = GitHubClient(config.owner, config.repo)
        self.sessions = SessionManager(
            config.session_secret, config.session_ttl_seconds
        )
        self._key_locks: dict[str, threading.Lock] = {}
        self._key_locks_guard = threading.Lock()
        validate_production_config(config)

    @contextmanager
    def _lock_for(self, key: str):
        with self._key_locks_guard:
            lock = self._key_locks.setdefault(key, threading.Lock())
        with lock:
            yield

    def _installation_token(self) -> str:
        if not self.config.github_configured:
            raise ServiceError(
                "github_unconfigured",
                "the submission service is not configured with a GitHub App",
                503,
            )
        return self.github.get_installation_token(
            self.config.app_id,
            self.config.installation_id,
            self.config.private_key_pem(),
        )

    def authenticate_github(self, access_token: str,
                            client_address: str) -> dict:
        if not self.auth_rate_limiter.allow(f"ip:{client_address}"):
            raise ServiceError("rate_limited", "auth rate limit exceeded", 429)
        if not access_token:
            raise ServiceError(
                "auth_required",
                "Authorization Bearer <GitHub access token> is required",
                401,
            )
        try:
            user = self.github.get_user(access_token)
        except GitHubApiError as exc:
            raise ServiceError(
                "auth_failed", "GitHub identity verification failed", 401
            ) from exc
        login = user.get("login", "")
        user_id = user.get("id")
        if not isinstance(login, str) or not login or user_id is None:
            raise ServiceError(
                "auth_failed", "GitHub identity has no login or id", 401
            )
        if not self.auth_rate_limiter.allow(f"user:{login}"):
            raise ServiceError("rate_limited", "auth rate limit exceeded", 429)
        token = self.sessions.issue(login, str(user_id))
        expires_at = datetime.now(timezone.utc).timestamp() + self.config.session_ttl_seconds
        return {
            "token": token,
            "expiresAt": datetime.fromtimestamp(
                expires_at, timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "login": login,
            "userId": str(user_id),
        }

    def _require_session(self, authorization: str) -> dict:
        if not authorization.startswith("Bearer "):
            raise ServiceError(
                "auth_required",
                "Authorization Bearer <submission session token> is required",
                401,
            )
        claims = self.sessions.verify(authorization[len("Bearer "):].strip())
        if claims is None:
            raise ServiceError(
                "auth_invalid",
                "submission session token is invalid or expired",
                401,
            )
        return claims

    def _authorize_owner(self, submission: dict, authorization: str) -> bool:
        if self.config.service_token and authorization == f"Bearer {self.config.service_token}":
            return True
        if not authorization.startswith("Bearer "):
            return False
        claims = self.sessions.verify(authorization[len("Bearer "):].strip())
        return claims is not None and claims.get("login") == submission.get("owner")

    def _find_by_idempotency(self, key_hash: str) -> dict | None:
        for path in self.config.storage_dir.glob("*.json"):
            try:
                submission = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if submission.get("idempotencyKeyHash") == key_hash:
                return submission
        return None

    def _find_by_content_hash(self, content_hash: str,
                              ignore_submission_id: str = "") -> dict | None:
        active = {"starting", "release_created", "asset_uploaded",
                  "branch_created", "manifest_written", "pr_created", "pending"}
        for path in self.config.storage_dir.glob("*.json"):
            try:
                submission = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (submission.get("id") != ignore_submission_id
                    and submission.get("contentHash") == content_hash
                    and submission.get("status") in active):
                return submission
        return None

    def create_submission(self, parsed, client_address: str,
                          idempotency_key: str, authorization: str) -> tuple[dict, bool]:
        claims = self._require_session(authorization)
        login = claims.get("login", "")
        if not self.rate_limiter.allow(f"ip:{client_address}"):
            raise ServiceError("rate_limited", "submission rate limit exceeded", 429)
        if not self.rate_limiter.allow(f"user:{login}"):
            raise ServiceError("rate_limited", "submission rate limit exceeded", 429)
        if not idempotency_key:
            idempotency_key = uuid.uuid4().hex
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        existing = self._find_by_idempotency(key_hash)
        if existing is not None:
            if existing.get("status") in ("pending", "failed", "cancelled"):
                return existing, True
        metadata, uploaded = self._parse_upload(parsed)
        if existing is not None:
            with self._lock_for(key_hash):
                existing = self._find_by_idempotency(key_hash)
                if existing is not None:
                    if existing.get("status") in ("pending", "failed", "cancelled"):
                        return existing, True
                    return self._process_submission(
                        metadata, uploaded, idempotency_key, key_hash, claims, existing
                    ), True
        with self._lock_for(key_hash):
            existing = self._find_by_idempotency(key_hash)
            if existing is not None:
                return existing, True
            return self._process_submission(
                metadata, uploaded, idempotency_key, key_hash, claims, None
            ), False

    def _parse_upload(self, parsed) -> tuple[dict, object]:
        metadata_text = parsed.fields.get("metadata", "")
        if not metadata_text:
            raise ServiceError("invalid_metadata", "metadata field is required", 400)
        try:
            metadata = json.loads(metadata_text)
        except json.JSONDecodeError as exc:
            raise ServiceError(
                "invalid_metadata",
                f"metadata is not valid JSON: {exc.msg}",
                400,
            ) from exc
        if not isinstance(metadata, dict):
            raise ServiceError("invalid_metadata", "metadata must be a JSON object", 400)
        uploaded = parsed.files.get("file")
        if uploaded is None:
            raise ServiceError("invalid_upload", "file field is required", 400)
        return metadata, uploaded

    def _process_submission(self, metadata: dict, uploaded,
                            idempotency_key: str, key_hash: str,
                            claims: dict, existing: dict | None) -> dict:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        login = claims.get("login", "")
        user_id = claims.get("sub", "")
        if not isinstance(user_id, str) or not USER_ID_RE.match(user_id):
            raise ServiceError(
                "auth_invalid", "session token has no numeric GitHub user id", 401
            )
        mid, version, name, summary, description, license_id = self._validate_metadata(
            metadata
        )
        if "githubToken" in metadata:
            raise ServiceError(
                "invalid_metadata",
                "githubToken must not be sent in metadata; use POST /v1/auth/github",
                400,
            )
        if not FILE_NAME_RE.match(uploaded.file_name or ""):
            raise ServiceError(
                "invalid_metadata",
                "file name must match ^[A-Za-z0-9._-]+\\.mascot$",
                422,
            )
        content_type = uploaded.content_type or "application/octet-stream"
        if not CONTENT_TYPE_RE.match(content_type):
            content_type = "application/octet-stream"

        package_ok, package_errors = validate_mascot(
            uploaded.temp_path,
            self.config.validator_cli,
            self.config.submission_env,
            self.config.validator_timeout_seconds,
        )
        if not package_ok:
            raise ServiceError(
                "package_invalid", "package failed validation", 422, package_errors
            )

        sha256 = hashlib.sha256(uploaded.temp_path.read_bytes()).hexdigest()
        content_hash = hashlib.sha256(
            f"{mid}|{version}|{sha256}".encode("utf-8")
        ).hexdigest()

        branch = f"submission/{mid}-{version}"
        if len(branch.encode("utf-8")) > MAX_BRANCH_LENGTH:
            raise ServiceError(
                "invalid_metadata", "id/version produce an overlong branch name", 422
            )
        manifest_path = f"mascots/{mid}/manifest.json"
        tag = f"draft/{mid}-{version}"

        token_app = self._installation_token()
        existing_parsed, ownership = self._resolve_ownership(
            token_app, manifest_path, mid, version, metadata, login, user_id
        )

        # The registry version/ownership check must happen before content-hash
        # dedup: re-uploading the exact package of an already published version
        # is a duplicate/version error, not an idempotent replay of the old
        # submission. Content dedup remains safe for new ids and for higher
        # versions whose first attempt created resources.
        if existing is None:
            duplicate = self._find_by_content_hash(content_hash)
            if duplicate is not None:
                return duplicate

        if existing is None:
            submission: dict = {
                "id": uuid.uuid4().hex[:24],
                "status": "starting",
                "owner": login,
                "ownerUserId": ownership["ownerUserId"],
                "ownerLogin": ownership["ownerLogin"],
                "maintainerUserIds": ownership["maintainerUserIds"],
                "mascotId": mid,
                "version": version,
                "createdAt": now,
                "updatedAt": now,
                "package": {
                    "fileName": uploaded.file_name,
                    "size": uploaded.size,
                    "sha256": sha256,
                    "contentType": content_type,
                },
                "idempotencyKeyHash": key_hash,
                "contentHash": content_hash,
                "steps": {},
                "release": {},
                "pr": {},
                "branch": branch,
            }
        else:
            submission = existing
            submission.setdefault("steps", {})
            submission.setdefault("release", {})
            submission.setdefault("pr", {})
            submission["ownerUserId"] = ownership["ownerUserId"]
            submission["ownerLogin"] = ownership["ownerLogin"]
            submission["maintainerUserIds"] = ownership["maintainerUserIds"]
            submission["branch"] = branch
        self.store.save(submission)

        try:
            if submission["steps"].get("release") != "done":
                release = self._get_or_create_release(
                    token_app, tag, mid, version, login, summary
                )
                submission["release"] = {
                    "releaseId": release.get("id"),
                    "tag": tag,
                }
                submission["steps"]["release"] = "done"
                submission["status"] = "release_created"
                self.store.save(submission)

            if submission["steps"].get("asset") != "done":
                asset = self._get_or_create_asset(
                    token_app, submission["release"]["releaseId"],
                    uploaded, uploaded.file_name, content_type,
                )
                submission["release"]["assetId"] = asset.get("id")
                submission["release"]["assetUrl"] = asset.get("browser_download_url", "")
                submission["steps"]["asset"] = "done"
                submission["status"] = "asset_uploaded"
                self.store.save(submission)

            if submission["steps"].get("branch") != "done":
                base_sha = self.github.get_branch_head_sha(token_app)
                self._get_or_create_branch(token_app, branch, base_sha)
                submission["steps"]["branch"] = "done"
                submission["status"] = "branch_created"
                self.store.save(submission)

            manifest = self._build_manifest(
                metadata, mid, version, sha256, uploaded, content_type,
                submission, now, ownership,
            )
            if submission["steps"].get("manifest") != "done":
                self._write_manifest(
                    token_app, manifest_path, manifest, branch
                )
                submission["steps"]["manifest"] = "done"
                submission["status"] = "manifest_written"
                self.store.save(submission)

            if submission["steps"].get("pr") != "done":
                title = _clean_text(
                    f"Mascot: {manifest['name']} ({mid} v{version})",
                    MAX_TITLE_LENGTH,
                )
                body = _clean_text(
                    f"Submission `{submission['id']}`\n\n{manifest['summary']}",
                    MAX_PR_BODY_LENGTH,
                    keep_newlines=True,
                )
                pr = self._get_or_create_pr(token_app, branch, title, body)
                submission["pr"] = {
                    "number": pr.get("number"),
                    "url": pr.get("html_url", ""),
                }
                submission["steps"]["pr"] = "done"
                submission["status"] = "pr_created"
                self.store.save(submission)

            changed_files = self.github.get_pull_request_files(
                token_app, submission["pr"]["number"]
            )
            if changed_files != [manifest_path]:
                self._compensate(
                    submission, token_app, reason="untrusted_changed_files"
                )
                submission["status"] = "failed"
                submission["error"] = {
                    "code": "untrusted_changes",
                    "message": "PR contains files outside the submission allowlist",
                }
                submission["updatedAt"] = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                self.store.save(submission)
                raise ServiceError(
                    "untrusted_changes",
                    "PR contains files outside the submission allowlist",
                    422,
                )

            pr_view = self.github.get_pull_request(
                token_app, submission["pr"]["number"]
            )
            head = pr_view.get("head") or {}
            head_sha = head.get("sha", "")
            head_repo = (head.get("repo") or {}).get("full_name", "")
            if not isinstance(head_sha, str) or not head_sha:
                raise ServiceError(
                    "submission_failed", "PR head SHA is missing", 502
                )
            if head_repo != f"{self.config.owner}/{self.config.repo}":
                raise ServiceError(
                    "untrusted_changes",
                    "PR head repository is not the official repository",
                    422,
                )
            submission["pr"]["headSha"] = head_sha
            submission["pr"]["headRepo"] = head_repo
            self.store.save(submission)

            if submission["steps"].get("checkrun") != "done":
                self._run_package_validation_check(
                    token_app, submission, manifest, manifest_path, mid,
                    version, head_sha,
                )
                submission["steps"]["checkrun"] = "done"
                submission["status"] = "package_validated"
                self.store.save(submission)

            submission["status"] = "pending"
            submission["updatedAt"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            self.store.save(submission)
            return submission
        except ServiceError:
            raise
        except GitHubApiError as exc:
            submission["error"] = {
                "code": exc.code,
                "message": redact_text(str(exc)),
            }
            submission["updatedAt"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            if not exc.retryable:
                self._compensate(submission, token_app, reason="submission_failed")
                submission["status"] = "failed"
            self.store.save(submission)
            raise ServiceError(
                "submission_failed",
                "the submission could not be completed; retry with the same "
                "idempotency key to resume",
                502,
            ) from exc

    def _resolve_ownership(self, token_app: str, manifest_path: str, mid: str,
                           version: str, metadata: dict, login: str,
                           user_id: str) -> tuple:
        """Resolve numeric-id based ownership and maintainer permissions.

        Returns (existing_parsed_or_None, ownership) where ownership is:
          {
            "ownerUserId": str,
            "ownerLogin": str,
            "maintainerUserIds": [str],
            "maintainers": [str],  # display logins, same index as the ids
            "authors": [dict],     # authors with the own login refreshed
          }
        The numeric GitHub user id is the permission basis; login is only
        used for display and is refreshed from the session when the id
        belongs to the authenticated user.
        """
        existing_manifest = self.github.get_file(token_app, manifest_path)
        if existing_manifest is None:
            authors = metadata.get("authors", [])
            first_author = authors[0] if authors else {}
            if not isinstance(first_author, dict) or first_author.get("githubLogin") != login:
                raise ServiceError(
                    "identity_mismatch",
                    "authors[0].githubLogin must match the authenticated GitHub user",
                    403,
                )
            if str(first_author.get("githubUserId", "")) != user_id:
                raise ServiceError(
                    "identity_mismatch",
                    "authors[0].githubUserId must match the authenticated "
                    "numeric GitHub user id",
                    403,
                )
            if metadata.get("maintainers") != [login]:
                raise ServiceError(
                    "initial_maintainer_mismatch",
                    "a new mascot must list only the submitter as maintainer",
                    403,
                )
            ownership = {
                "ownerUserId": user_id,
                "ownerLogin": login,
                "maintainerUserIds": [user_id],
                "maintainers": [login],
                "authors": self._normalize_identity_logins(
                    authors, user_id, login
                ),
            }
            return None, ownership

        try:
            existing_text = base64.b64decode(
                existing_manifest.get("content", "")
            ).decode("utf-8")
            existing_parsed = json.loads(existing_text)
        except (KeyError, json.JSONDecodeError):
            raise ServiceError(
                "registry_unreadable", "existing manifest is unreadable", 500
            ) from None
        if existing_parsed.get("version") == version:
            raise ServiceError(
                "duplicate_id_version",
                f"id {mid!r} version {version!r} already exists",
                409,
            )
        if not isinstance(existing_parsed.get("version"), str) or not semver_gt(
            version, existing_parsed["version"]
        ):
            raise ServiceError(
                "version_not_higher",
                f"new version {version!r} must be strictly higher than the "
                f"published version {existing_parsed.get('version')!r}",
                409,
            )
        maintainer_ids = existing_parsed.get("maintainerUserIds")
        if not isinstance(maintainer_ids, list) or not maintainer_ids or any(
            not isinstance(item, str) or not USER_ID_RE.match(item)
            for item in maintainer_ids
        ):
            raise ServiceError(
                "legacy_manifest_no_user_ids",
                "existing manifest has no numeric maintainer ids; update "
                "requires maintainer approval",
                403,
            )
        if len(set(maintainer_ids)) != len(maintainer_ids):
            raise ServiceError(
                "registry_maintainer_ids_invalid",
                "existing manifest has duplicate numeric maintainer ids",
                500,
            )
        if user_id not in maintainer_ids:
            raise ServiceError(
                "non_maintainer",
                "the authenticated user is not a maintainer of this mascot",
                403,
            )
        existing_maintainers = existing_parsed.get("maintainers")
        if (not isinstance(existing_maintainers, list)
                or any(not isinstance(item, str) for item in existing_maintainers)
                or len(existing_maintainers) != len(maintainer_ids)):
            raise ServiceError(
                "registry_maintainers_invalid",
                "existing maintainers do not align with maintainerUserIds",
                500,
            )
        submitted_maintainers = metadata.get("maintainers")
        if (not isinstance(submitted_maintainers, list)
                or len(submitted_maintainers) != len(maintainer_ids)):
            raise ServiceError(
                "maintainers_change_requires_approval",
                "changing the set of maintainers requires existing maintainer "
                "or administrator approval",
                403,
            )
        # maintainers[i] is the display login for maintainerUserIds[i].
        # Membership is decided only by the numeric id set; the authenticated
        # user may refresh only the login of their own numeric id.
        normalized_maintainers: list[str] = []
        for index, maintainer_id in enumerate(maintainer_ids):
            submitted_login = submitted_maintainers[index]
            if maintainer_id == user_id:
                normalized_maintainers.append(login)
                continue
            if submitted_login != existing_maintainers[index]:
                raise ServiceError(
                    "maintainers_change_requires_approval",
                    "you may only update your own login; changing another "
                    "maintainer's login requires approval",
                    403,
                )
            normalized_maintainers.append(submitted_login)
        authors = metadata.get("authors", [])
        first_author = authors[0] if authors else {}
        if (not isinstance(first_author, dict)
                or str(first_author.get("githubUserId", "")) != user_id):
            raise ServiceError(
                "identity_mismatch",
                "authors[0].githubUserId must match the authenticated "
                "numeric GitHub user id",
                403,
            )
        owner_obj = existing_parsed.get("owner")
        if not isinstance(owner_obj, dict) or not isinstance(
            owner_obj.get("userId"), str
        ) or not USER_ID_RE.match(owner_obj["userId"]):
            raise ServiceError(
                "registry_owner_invalid",
                "existing manifest owner is invalid",
                500,
            )
        owner_user_id = owner_obj["userId"]
        owner_login = owner_obj.get("login", "")
        if owner_user_id == user_id:
            owner_login = login
        ownership = {
            "ownerUserId": owner_user_id,
            "ownerLogin": owner_login,
            "maintainerUserIds": maintainer_ids,
            "maintainers": normalized_maintainers,
            "authors": self._normalize_identity_logins(
                authors, user_id, login
            ),
        }
        return existing_parsed, ownership

    def _normalize_identity_logins(self, authors: list, user_id: str,
                                   login: str) -> list:
        """Return authors with githubLogin refreshed for the given user id."""
        normalized = []
        for author in authors:
            if not isinstance(author, dict):
                normalized.append(author)
                continue
            entry = dict(author)
            if str(entry.get("githubUserId", "")) == user_id:
                entry["githubLogin"] = login
            normalized.append(entry)
        return normalized

    def _validate_metadata(self, metadata: dict) -> tuple:
        for forbidden in ("owner", "maintainerUserIds", "status"):
            if forbidden in metadata:
                raise ServiceError(
                    "invalid_metadata",
                    f"metadata must not contain {forbidden!r}; the service "
                    "derives it from the authenticated identity and the registry",
                    422,
                )
        mid = metadata.get("id")
        version = metadata.get("version")
        if not isinstance(mid, str) or not ID_RE.match(mid):
            raise ServiceError("invalid_metadata", "id is invalid", 422)
        if len(mid) > MAX_ID_LENGTH:
            raise ServiceError(
                "invalid_metadata", f"id exceeds {MAX_ID_LENGTH} characters", 422
            )
        if mid in RESERVED_IDS:
            raise ServiceError("invalid_metadata", "id is a reserved name", 422)
        if mid != mid.lower():
            raise ServiceError("invalid_metadata", "id must be lowercase", 422)
        if not isinstance(version, str) or not SEMVER_RE.match(version):
            raise ServiceError("invalid_metadata", "version must be valid SemVer", 422)
        if len(version) > MAX_VERSION_LENGTH:
            raise ServiceError(
                "invalid_metadata", f"version exceeds {MAX_VERSION_LENGTH} characters", 422
            )
        if any(ord(char) < 32 for char in mid + version):
            raise ServiceError(
                "invalid_metadata", "id/version contain control characters", 422
            )
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

        authors = metadata.get("authors")
        maintainers = metadata.get("maintainers")
        if not isinstance(authors, list) or not authors:
            raise ServiceError("invalid_metadata", "authors must be a non-empty array", 422)
        if not isinstance(maintainers, list) or not maintainers:
            raise ServiceError("invalid_metadata", "maintainers must be a non-empty array", 422)
        if any(not isinstance(entry, str) or not LOGIN_RE.match(entry)
               for entry in maintainers):
            raise ServiceError("invalid_metadata", "maintainers contain an invalid login", 422)
        first_author = authors[0]
        if not isinstance(first_author, dict):
            raise ServiceError(
                "invalid_metadata", "authors[0] must be an object", 422
            )
        if not isinstance(first_author.get("githubLogin"), str) or not LOGIN_RE.match(
            first_author.get("githubLogin", "")
        ):
            raise ServiceError(
                "invalid_metadata", "authors[0].githubLogin is invalid", 422
            )
        if not isinstance(first_author.get("githubUserId"), str) or not USER_ID_RE.match(
            first_author.get("githubUserId", "")
        ):
            raise ServiceError(
                "invalid_metadata", "authors[0].githubUserId is invalid", 422
            )
        tags = metadata.get("tags", [])
        categories = metadata.get("categories", [])
        if not isinstance(tags, list) or any(
            not isinstance(item, str) or len(item) > 64 for item in tags
        ):
            raise ServiceError("invalid_metadata", "tags are invalid", 422)
        if not isinstance(categories, list) or any(
            not isinstance(item, str) or len(item) > 64 for item in categories
        ):
            raise ServiceError("invalid_metadata", "categories are invalid", 422)
        upstream = metadata.get("upstream", "")
        if not isinstance(upstream, str) or len(upstream) > 2000:
            raise ServiceError("invalid_metadata", "upstream is invalid", 422)
        minimum = metadata.get("minimumNeurolingsCEVersion", "0.5.1")
        if not isinstance(minimum, str) or len(minimum) > 64:
            raise ServiceError(
                "invalid_metadata", "minimumNeurolingsCEVersion is invalid", 422
            )
        return mid, version, name, summary, description, license_id

    def _get_or_create_release(self, token: str, tag: str, mid: str,
                               version: str, login: str, summary: str) -> dict:
        existing = self.github.get_release_by_tag(token, tag)
        if existing is not None:
            if not existing.get("draft"):
                raise ServiceError(
                    "duplicate_release",
                    "a non-draft release already exists for this tag",
                    409,
                )
            return existing
        try:
            return self.github.create_draft_release(
                token,
                tag,
                f"Mascot {mid} v{version}",
                f"Submission by @{login}: {_clean_text(summary, 300)}",
            )
        except GitHubApiError as exc:
            if exc.status in (409, 422):
                existing = self.github.get_release_by_tag(token, tag)
                if existing is not None:
                    return existing
            raise

    def _get_or_create_asset(self, token: str, release_id: int, uploaded,
                             file_name: str, content_type: str) -> dict:
        assets = self.github.get_release_assets(token, release_id)
        for asset in assets:
            if asset.get("name") == file_name:
                return asset
        try:
            return self.github.upload_release_asset(
                token, release_id, uploaded.temp_path, file_name, content_type
            )
        except GitHubApiError as exc:
            if exc.status in (409, 422):
                assets = self.github.get_release_assets(token, release_id)
                for asset in assets:
                    if asset.get("name") == file_name:
                        return asset
            raise

    def _get_or_create_branch(self, token: str, branch: str, base_sha: str) -> dict:
        existing = self.github.get_branch_ref(token, branch)
        if existing is not None:
            return existing
        try:
            return self.github.create_branch(token, branch, base_sha)
        except GitHubApiError as exc:
            if exc.status in (409, 422):
                existing = self.github.get_branch_ref(token, branch)
                if existing is not None:
                    return existing
            raise

    def _write_manifest(self, token: str, path: str, manifest: dict,
                        branch: str) -> dict:
        existing = self.github.get_file(token, path, ref=branch)
        sha = existing.get("sha") if existing is not None else None
        try:
            return self.github.create_or_update_file(
                token,
                path,
                f"Add {manifest['id']} v{manifest['version']} via submission "
                f"{manifest.get('submissionId', '')}".strip(),
                json.dumps(manifest, indent=2, sort_keys=True),
                branch,
                sha,
            )
        except GitHubApiError as exc:
            if exc.status in (409, 422):
                existing = self.github.get_file(token, path, ref=branch)
                if existing is not None:
                    return existing
            raise

    def _get_or_create_pr(self, token: str, branch: str, title: str,
                          body: str) -> dict:
        existing = self.github.get_pull_request_by_head(token, branch)
        if existing is not None:
            return existing
        try:
            return self.github.create_pull_request(
                token, title, branch, "main", body
            )
        except GitHubApiError as exc:
            if exc.status in (409, 422):
                existing = self.github.get_pull_request_by_head(token, branch)
                if existing is not None:
                    return existing
            raise

    def _fail_submission(self, submission: dict, code: str, message: str,
                         details=None) -> None:
        submission["status"] = "failed"
        error: dict = {
            "code": code,
            "message": _clean_text(redact_text(message), 1000),
        }
        if details is not None:
            if isinstance(details, list):
                error["details"] = [
                    _clean_text(redact_text(str(item)), 500)
                    for item in details
                ]
            else:
                error["details"] = _clean_text(
                    redact_text(str(details)), 500
                )
        submission["error"] = error
        submission["updatedAt"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self.store.save(submission)

    def _complete_check_run(self, token_app: str, check_run_id: int,
                            conclusion: str, summary: str) -> None:
        try:
            self.github.update_check_run(
                token_app, check_run_id,
                status="completed", conclusion=conclusion,
                output={
                    "title": "Mascot package validation",
                    "summary": _clean_text(
                        summary, 65535, keep_newlines=True
                    ),
                },
            )
        except GitHubApiError as exc:
            LOGGER.error(
                "check run update failed: %s", redact_text(str(exc))
            )

    def _run_package_validation_check(self, token_app: str, submission: dict,
                                      manifest: dict, manifest_path: str,
                                      mid: str, version: str,
                                      head_sha: str) -> None:
        """Create/update the Publisher App package-validation Check Run.

        The check is bound to the final PR head SHA; on any failure the run
        is completed with conclusion=failure and the submission is failed.
        Retries with the same idempotency key reuse the existing check run
        (identified by name + external id on the same head SHA).
        """
        submission_id = submission["id"]
        external_id = f"neurolingsce-submission:{submission_id}:{head_sha}"
        check_run_id: int | None = None
        try:
            existing = [
                run for run in self.github.get_check_runs_for_head(
                    token_app, head_sha
                )
                if run.get("name") == "package-validation"
                and run.get("external_id") == external_id
            ]
            if existing:
                check_run_id = existing[0].get("id")
            else:
                created = self.github.create_check_run(
                    token_app, head_sha, "package-validation", external_id
                )
                check_run_id = created.get("id")
            if not isinstance(check_run_id, int):
                raise ServiceError(
                    "package_validation_failed",
                    "check run was created without an id",
                    502,
                )
            submission["checkRun"] = {
                "id": check_run_id,
                "name": "package-validation",
                "externalId": external_id,
                "headSha": head_sha,
            }
            self.store.save(submission)
        except (GitHubApiError, ServiceError) as exc:
            self._fail_submission(
                submission, "package_validation_failed",
                "package-validation check run could not be created; the PR "
                "stays unmergeable; retry with the same idempotency key",
            )
            raise ServiceError(
                "package_validation_failed",
                "package-validation check run could not be created; retry "
                "with the same idempotency key",
                502,
            ) from exc

        try:
            self._verify_and_revalidate_package(
                token_app, submission, manifest, manifest_path, mid, version,
                head_sha, check_run_id,
            )
        except ServiceError as exc:
            details = exc.details if isinstance(exc.details, list) else []
            summary = (
                f"Submission {submission_id} mascot {mid} {version} failed: "
                f"{exc.code}"
            )
            if details:
                summary += " — " + "; ".join(str(item) for item in details)
            self._complete_check_run(token_app, check_run_id, "failure", summary)
            self._fail_submission(submission, exc.code, str(exc), details)
            raise

    def _verify_and_revalidate_package(self, token_app: str, submission: dict,
                                       manifest: dict, manifest_path: str,
                                       mid: str, version: str, head_sha: str,
                                       check_run_id: int) -> None:
        """Download the exact draft asset and revalidate it with the CLI."""
        meta = manifest.get("release") or {}
        release_id = meta.get("releaseId")
        asset_id = meta.get("assetId")
        tag = meta.get("tag", "")
        package = manifest.get("package") or {}
        file_name = package.get("fileName", "")
        sha256 = package.get("sha256", "")
        size = package.get("size", 0)
        if not isinstance(release_id, int) or not isinstance(asset_id, int):
            raise ServiceError(
                "package_validation_failed",
                "manifest release.releaseId/assetId must be integers",
                422,
            )
        if not isinstance(tag, str) or tag != f"draft/{mid}-{version}":
            raise ServiceError(
                "package_validation_failed",
                "manifest release.tag does not match the submission",
                422,
            )
        if not isinstance(file_name, str) or not FILE_NAME_RE.match(file_name):
            raise ServiceError(
                "package_validation_failed",
                "manifest package.fileName is invalid",
                422,
            )
        if not isinstance(sha256, str) or not re.match(
            r"^[0-9a-f]{64}$", sha256
        ):
            raise ServiceError(
                "package_validation_failed",
                "manifest package.sha256 is invalid",
                422,
            )
        if not isinstance(size, int) or size < 1:
            raise ServiceError(
                "package_validation_failed",
                "manifest package.size is invalid",
                422,
            )

        release = self.github.get_release(token_app, release_id)
        if release is None or release.get("draft") is not True:
            raise ServiceError(
                "package_validation_failed",
                "release is not an existing draft",
                422,
            )
        if release.get("tag_name") != tag:
            raise ServiceError(
                "package_validation_failed",
                "release tag does not match the manifest",
                422,
            )
        assets = self.github.get_release_assets(token_app, release_id)
        asset = next(
            (item for item in assets if item.get("id") == asset_id), None
        )
        if (asset is None or asset.get("name") != file_name
                or asset.get("state") != "uploaded"):
            raise ServiceError(
                "package_validation_failed",
                "asset does not match the manifest",
                422,
            )

        pr = self.github.get_pull_request(
            token_app, submission["pr"]["number"]
        )
        current_head = (pr.get("head") or {}).get("sha", "")
        if pr.get("state") != "open" or current_head != head_sha:
            raise ServiceError(
                "package_validation_failed",
                "PR head SHA changed during validation",
                422,
            )
        files = self.github.get_pull_request_files(
            token_app, submission["pr"]["number"]
        )
        if files != [manifest_path]:
            raise ServiceError(
                "package_validation_failed",
                "PR changed files changed during validation",
                422,
            )

        with tempfile.TemporaryDirectory(prefix="neurolingsce-check-") as tmp:
            dest = Path(tmp) / file_name
            try:
                self.github.download_release_asset(
                    token_app, release_id, asset_id, dest, sha256, size
                )
            except GitHubApiError as exc:
                if exc.retryable:
                    raise
                raise ServiceError(
                    "package_validation_failed",
                    f"draft asset download failed: {exc.code}",
                    422,
                ) from exc
            ok, errors, report = validate_mascot_detailed(
                dest,
                self.config.validator_cli,
                self.config.submission_env,
                self.config.validator_timeout_seconds,
            )
            if not ok:
                raise ServiceError(
                    "package_validation_failed",
                    "package revalidation failed",
                    422,
                    errors,
                )
            if (report.get("package_version") is not None
                    and report.get("package_version") != version):
                raise ServiceError(
                    "package_validation_failed",
                    "validator identified a package version that does not "
                    "match the manifest",
                    422,
                )

        self._complete_check_run(
            token_app, check_run_id, "success",
            f"Submission {submission['id']} mascot {mid} {version} "
            f"validated: size {size} bytes; sha256 {sha256[:12]}…; "
            f"validator ok",
        )

    def _build_manifest(self, metadata: dict, mid: str, version: str,
                        sha256: str, uploaded, content_type: str,
                        submission: dict, now: str, ownership: dict) -> dict:
        release_tag = submission.get("release", {}).get("tag", "")
        asset_url = (
            f"https://github.com/{self.config.owner}/{self.config.repo}"
            f"/releases/download/{release_tag}/{uploaded.file_name}"
        )
        return {
            "schemaVersion": "1",
            "id": mid,
            "name": metadata["name"],
            "version": version,
            "summary": metadata["summary"],
            "description": metadata["description"],
            "authors": ownership["authors"],
            "maintainers": ownership["maintainers"],
            "maintainerUserIds": ownership["maintainerUserIds"],
            "owner": {
                "userId": ownership["ownerUserId"],
                "login": ownership["ownerLogin"],
            },
            "license": metadata["license"],
            "isDerivative": bool(metadata.get("isDerivative", False)),
            "upstream": metadata.get("upstream", ""),
            "minimumNeurolingsCEVersion": metadata.get(
                "minimumNeurolingsCEVersion", "0.5.1"
            ),
            "package": {
                "fileName": uploaded.file_name,
                "url": asset_url,
                "size": uploaded.size,
                "sha256": sha256,
                "contentType": content_type,
            },
            "tags": metadata.get("tags", []),
            "categories": metadata.get("categories", []),
            "createdAt": now,
            "updatedAt": now,
            "release": {
                "releaseId": submission["release"].get("releaseId"),
                "assetId": submission["release"].get("assetId"),
                "tag": submission["release"].get("tag"),
            },
            "submissionId": submission["id"],
        }

    def _compensate(self, submission: dict, token: str, reason: str) -> None:
        """Safely undo a failed submission without touching unrelated resources."""
        errors: list[str] = []
        pr = submission.get("pr") or {}
        branch = submission.get("branch", "")
        if pr.get("number"):
            try:
                live = self.github.get_pull_request_by_head(token, branch) if branch else None
                if live is not None and live.get("number") == pr["number"]:
                    self.github.close_pull_request(token, pr["number"])
            except GitHubApiError as exc:
                if exc.status != 404:
                    errors.append(f"close_pr:{exc.code}")
        if branch:
            try:
                self.github.delete_branch_ref(token, branch)
            except GitHubApiError as exc:
                if exc.status != 404:
                    errors.append(f"delete_branch:{exc.code}")
        release = submission.get("release") or {}
        release_id = release.get("releaseId")
        tag = release.get("tag")
        if release_id and tag:
            try:
                live = self.github.get_release(token, release_id)
                if live is not None and live.get("draft") is True and live.get("tag_name") == tag:
                    self.github.delete_release(token, release_id)
            except GitHubApiError as exc:
                if exc.status != 404:
                    errors.append(f"delete_release:{exc.code}")
        submission["compensation"] = {"reason": reason, "errors": errors}

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
            self._compensate(submission, token, reason="user_cancel")
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

    def _authorization(self) -> str:
        return self.headers.get("Authorization", "")

    def do_POST(self) -> None:
        if self.path == "/v1/auth/github":
            try:
                result = self.service.authenticate_github(
                    self._authorization().removeprefix("Bearer ").strip(),
                    self.client_address[0],
                )
                LOGGER.info(
                    "github auth success login=%s expiresAt=%s",
                    result["login"], result["expiresAt"],
                )
                self._send(200, result)
            except Exception as exc:  # noqa: BLE001
                self._send_error(exc)
            return
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
                self._authorization(),
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
                match.group(1), self._authorization()
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
                match.group(1), self._authorization()
            )
            self._send(200, {"id": result["id"], "status": result["status"]})
        except Exception as exc:  # noqa: BLE001
            self._send_error(exc)


def build_service(config: Config | None = None) -> SubmissionService:
    config = config or Config()
    return SubmissionService(config)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    try:
        config = Config()
    except ValueError as exc:
        LOGGER.error("invalid configuration: %s", exc)
        return 2
    try:
        validate_production_config(config)
    except RuntimeError as exc:
        LOGGER.error("production startup self-check failed: %s", exc)
        return 2
    Handler.service = build_service(config)
    server = ThreadingHTTPServer(("0.0.0.0", config.port), Handler)
    LOGGER.info(
        "submission service listening port=%s storage=%s github_configured=%s "
        "env=%s validator=%s",
        config.port, config.storage_dir, config.github_configured,
        config.submission_env,
        "required" if config.production else ("configured" if config.validator_cli else "not-configured"),
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
