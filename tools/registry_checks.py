"""Shared registry validation rules (stdlib-only).

These checks are the source of truth for the NeurolingsCE-Mascots repository.
The authoritative binary package validation remains the C++ CLI
(NeurolingsCE-cli --mascot validate); this module validates registry
metadata, ids, versions, and consistency.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$"
)
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ISO8601_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)

REQUIRED_MANIFEST_FIELDS = (
    "schemaVersion",
    "id",
    "name",
    "version",
    "summary",
    "description",
    "authors",
    "maintainers",
    "license",
    "minimumNeurolingsCEVersion",
    "package",
    "createdAt",
    "updatedAt",
)


def _is_str(value) -> bool:
    return isinstance(value, str)


def validate_manifest_text(text: str, path_hint: str = "manifest.json") -> list[str]:
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"{path_hint}: invalid JSON ({exc.msg})"]
    return validate_manifest_object(manifest, path_hint=path_hint)


def validate_manifest_object(manifest, path_hint: str = "manifest.json") -> list[str]:
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return [f"{path_hint}: manifest must be a JSON object"]

    for field in REQUIRED_MANIFEST_FIELDS:
        if field not in manifest:
            errors.append(f"{path_hint}: missing required field {field}")
    if "schemaVersion" in manifest and manifest["schemaVersion"] != "1":
        errors.append(f"{path_hint}: schemaVersion must be \"1\"")

    mid = manifest.get("id")
    if _is_str(mid):
        if len(mid) > 64 or not ID_RE.match(mid):
            errors.append(
                f"{path_hint}: id must match ^[a-z0-9]+(-[a-z0-9]+)*$ and be <= 64 chars"
            )
    elif "id" in manifest:
        errors.append(f"{path_hint}: id must be a string")

    version = manifest.get("version")
    if _is_str(version) and not SEMVER_RE.match(version):
        errors.append(f"{path_hint}: version must be valid SemVer (got {version!r})")
    minimum = manifest.get("minimumNeurolingsCEVersion")
    if _is_str(minimum) and not SEMVER_RE.match(minimum):
        errors.append(
            f"{path_hint}: minimumNeurolingsCEVersion must be valid SemVer"
        )

    for field in ("name", "summary", "description", "license"):
        value = manifest.get(field)
        if value is not None and not _is_str(value):
            errors.append(f"{path_hint}: {field} must be a string")

    license_value = manifest.get("license")
    if license_value is not None:
        if _is_str(license_value) and (
            len(license_value) > 64
            or not re.match(r"^[A-Za-z0-9.+-]+$", license_value)
        ):
            errors.append(f"{path_hint}: license must be a compact SPDX identifier")

    authors = manifest.get("authors")
    if isinstance(authors, list):
        if not authors:
            errors.append(f"{path_hint}: authors must not be empty")
        for index, author in enumerate(authors):
            if not isinstance(author, dict):
                errors.append(f"{path_hint}: authors[{index}] must be an object")
                continue
            login = author.get("githubLogin")
            display = author.get("displayName")
            if not _is_str(login) or not LOGIN_RE.match(login) or len(login) > 39:
                errors.append(
                    f"{path_hint}: authors[{index}].githubLogin is invalid"
                )
            if not _is_str(display) or not display.strip():
                errors.append(
                    f"{path_hint}: authors[{index}].displayName must be non-empty"
                )
    elif "authors" in manifest:
        errors.append(f"{path_hint}: authors must be an array")

    maintainers = manifest.get("maintainers")
    if isinstance(maintainers, list):
        if not maintainers:
            errors.append(f"{path_hint}: maintainers must not be empty")
        for index, login in enumerate(maintainers):
            if not _is_str(login) or not LOGIN_RE.match(login) or len(login) > 39:
                errors.append(f"{path_hint}: maintainers[{index}] is invalid")
    elif "maintainers" in manifest:
        errors.append(f"{path_hint}: maintainers must be an array")

    maintainer_ids = manifest.get("maintainerUserIds")
    if maintainer_ids is not None:
        if not isinstance(maintainer_ids, list) or not maintainer_ids:
            errors.append(f"{path_hint}: maintainerUserIds must be a non-empty array")
        elif isinstance(maintainers, list) and len(maintainer_ids) != len(maintainers):
            errors.append(
                f"{path_hint}: maintainerUserIds length must equal maintainers length"
            )
        if isinstance(maintainer_ids, list):
            for index, user_id in enumerate(maintainer_ids):
                if not _is_str(user_id) or not re.match(r"^[0-9]{1,20}$", user_id):
                    errors.append(f"{path_hint}: maintainerUserIds[{index}] is invalid")
    if (isinstance(maintainers, list) and isinstance(maintainer_ids, list)
            and len(maintainers) == len(maintainer_ids)
            and all(_is_str(item) for item in maintainer_ids)
            and len(set(maintainer_ids)) != len(maintainer_ids)):
        errors.append(f"{path_hint}: maintainerUserIds must be unique")

    owner = manifest.get("owner")
    if owner is not None:
        if not isinstance(owner, dict):
            errors.append(f"{path_hint}: owner must be an object")
        else:
            user_id = owner.get("userId")
            owner_login = owner.get("login")
            if not _is_str(user_id) or not re.match(r"^[0-9]{1,20}$", user_id):
                errors.append(f"{path_hint}: owner.userId is invalid")
            if not _is_str(owner_login) or not LOGIN_RE.match(owner_login):
                errors.append(f"{path_hint}: owner.login is invalid")
        if isinstance(authors, list) and authors:
            first_author = authors[0]
            if (not isinstance(first_author, dict)
                    or not _is_str(first_author.get("githubUserId"))
                    or not re.match(r"^[0-9]{1,20}$", first_author.get("githubUserId", ""))):
                errors.append(
                    f"{path_hint}: authors[0].githubUserId is required for "
                    "new-format manifests"
                )

    if isinstance(owner, dict):
        owner_id = owner.get("userId")
        owner_login = owner.get("login")
        if (_is_str(owner_id) and _is_str(owner_login)
                and isinstance(maintainers, list)
                and isinstance(maintainer_ids, list)):
            for index, maintainer_id in enumerate(maintainer_ids):
                if maintainer_id == owner_id and index < len(maintainers):
                    if maintainers[index] != owner_login:
                        errors.append(
                            f"{path_hint}: owner.login must match "
                            f"maintainers[{index}] for numeric user id "
                            f"{owner_id}"
                        )

    if isinstance(authors, list):
        for index, author in enumerate(authors):
            if not isinstance(author, dict):
                continue
            author_id = author.get("githubUserId")
            author_login = author.get("githubLogin")
            if not _is_str(author_id) or not _is_str(author_login):
                continue
            if isinstance(owner, dict) and owner.get("userId") == author_id:
                if owner.get("login") != author_login:
                    errors.append(
                        f"{path_hint}: authors[{index}].githubLogin must match "
                        f"owner.login for numeric user id {author_id}"
                    )
            if isinstance(maintainer_ids, list) and isinstance(maintainers, list):
                for m_index, maintainer_id in enumerate(maintainer_ids):
                    if maintainer_id == author_id and m_index < len(maintainers):
                        if maintainers[m_index] != author_login:
                            errors.append(
                                f"{path_hint}: authors[{index}].githubLogin must "
                                f"match maintainers[{m_index}] for numeric user "
                                f"id {author_id}"
                            )

    package = manifest.get("package")
    if isinstance(package, dict):
        for field in ("fileName", "url", "size", "sha256"):
            if field not in package:
                errors.append(f"{path_hint}: package missing {field}")
        sha = package.get("sha256")
        if _is_str(sha) and not SHA256_RE.match(sha):
            errors.append(f"{path_hint}: package.sha256 must be 64 lowercase hex chars")
        size = package.get("size")
        if isinstance(size, int) and size < 1:
            errors.append(f"{path_hint}: package.size must be >= 1")
        file_name = package.get("fileName")
        if _is_str(file_name) and not re.match(
            r"^[A-Za-z0-9._-]+\.mascot$", file_name
        ):
            errors.append(
                f"{path_hint}: package.fileName must match "
                "^[A-Za-z0-9._-]+\\.mascot$"
            )
    elif "package" in manifest:
        errors.append(f"{path_hint}: package must be an object")

    for field in ("createdAt", "updatedAt"):
        value = manifest.get(field)
        if _is_str(value) and not ISO8601_RE.match(value):
            errors.append(f"{path_hint}: {field} must be ISO-8601 UTC")

    status = manifest.get("status")
    if status is not None and status not in ("published", "draft"):
        errors.append(f"{path_hint}: status must be published or draft")

    return errors


def iter_manifest_paths(root: Path):
    mascots_dir = root / "mascots"
    if not mascots_dir.is_dir():
        return
    for child in sorted(mascots_dir.iterdir()):
        if not child.is_dir():
            continue
        manifest_path = child / "manifest.json"
        if manifest_path.is_file():
            yield child.name, manifest_path


def load_manifest(manifest_path: Path) -> tuple[dict, list[str]]:
    errors: list[str] = []
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, [f"{manifest_path}: cannot read ({exc.strerror})"]
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, [f"{manifest_path}: invalid JSON ({exc.msg})"]
    if not isinstance(manifest, dict):
        return {}, [f"{manifest_path}: manifest must be a JSON object"]
    errors.extend(validate_manifest_object(manifest, path_hint=str(manifest_path)))
    return manifest, errors


def validate_registry(root: Path) -> list[str]:
    """Validate all manifests plus cross-manifest uniqueness rules."""
    errors: list[str] = []
    seen_ids: dict[str, Path] = {}
    seen_versions: dict[tuple[str, str], Path] = {}

    for directory_name, manifest_path in iter_manifest_paths(root):
        manifest, manifest_errors = load_manifest(manifest_path)
        errors.extend(manifest_errors)
        mid = manifest.get("id") if isinstance(manifest, dict) else None
        if _is_str(mid) and mid != directory_name:
            errors.append(
                f"{manifest_path}: id ({mid!r}) must equal the directory name "
                f"({directory_name!r})"
            )
        if _is_str(mid):
            if mid in seen_ids:
                errors.append(
                    f"{manifest_path}: duplicate id {mid!r} "
                    f"(first seen in {seen_ids[mid]})"
                )
            else:
                seen_ids[mid] = manifest_path
            version = manifest.get("version")
            if _is_str(version):
                key = (mid, version)
                if key in seen_versions:
                    errors.append(
                        f"{manifest_path}: duplicate id+version {mid!r} {version!r} "
                        f"(first seen in {seen_versions[key]})"
                    )
                else:
                    seen_versions[key] = manifest_path
    return errors


def load_index_entry(manifest: dict) -> dict:
    """Build the index entry for one published manifest."""
    authors = [
        author.get("githubLogin", "")
        for author in manifest.get("authors", [])
        if isinstance(author, dict) and isinstance(author.get("githubLogin"), str)
    ]
    package = manifest.get("package", {})
    entry = {
        "id": manifest["id"],
        "name": manifest["name"],
        "version": manifest["version"],
        "summary": manifest.get("summary", ""),
        "status": "published",
        "authors": authors,
        "maintainers": list(manifest.get("maintainers", [])),
        "license": manifest["license"],
        "minimumNeurolingsCEVersion": manifest.get("minimumNeurolingsCEVersion", ""),
        "download": {
            "url": package.get("url", ""),
            "size": package.get("size", 0),
            "sha256": package.get("sha256", ""),
        },
        "createdAt": manifest["createdAt"],
        "updatedAt": manifest["updatedAt"],
    }
    if manifest.get("tags"):
        entry["tags"] = list(manifest["tags"])
    if manifest.get("categories"):
        entry["categories"] = list(manifest["categories"])
    for key in ("icon", "previews"):
        value = manifest.get(key)
        if isinstance(value, dict):
            entry[key] = {
                "url": value.get("url", ""),
                "size": value.get("size", 0),
                "sha256": value.get("sha256", ""),
            }
        elif isinstance(value, list):
            entry[key] = [
                {
                    "url": item.get("url", ""),
                    "size": item.get("size", 0),
                    "sha256": item.get("sha256", ""),
                }
                for item in value
                if isinstance(item, dict)
            ]
    return entry
